#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git-guard scan — 扫描暂存区或待推送内容里的敏感业务数据。

用法:
  python scan.py --staged                  # pre-commit 调用
  python scan.py --message FILE            # commit-msg 调用（扫提交信息）
  python scan.py --range origin/main..HEAD # pre-push 调用（可多个 range）
  python scan.py --ranges-stdin            # pre-push 调用（含提交信息）
  python scan.py --all                     # 全历史体检（--all + --reflog）

退出码: 0 干净 / 1 命中 / 2 运行错误

外部文件（均只存本地，不入库）:
  ~/.git-guard/terms.txt   自定义敏感词，每行一个
  ~/.git-guard/allow.txt   允许的正则，每行一个（命中后二次过滤）
  环境变量可改用 GIT_GUARD_TERMS / GIT_GUARD_ALLOW 指定路径
"""
import argparse
import io
import os
import re
import subprocess
import sys
import zipfile

__version__ = "0.2.0"

# 单个对象参与解码的字节上限；超出部分只做 UTF-8 前段扫描并记为覆盖不足
MAX_BLOB_BYTES = int(os.environ.get("GIT_GUARD_MAX_BLOB", 32 * 1024 * 1024))
# 超限时保留尾部这一截：补齐/sparse 的大文件里，真正的内容常常压在末尾，
# 只留头段等于把「最后一个字节里的东西」全漏掉。
TAIL_KEEP = 2 * 1024 * 1024

GENERIC = {
    "ASIN": re.compile(r"\bB0[A-Z0-9]{8}\b"),
    "店铺名": re.compile(r"\d+\.[\u4e00-\u9fa5]{2,}-(?:DE|FR|ES|IT|UK|US|MX|CA|JP|AU|AE|SA|NL|BE|SE|PL|IE)\b"),
    # 金额/销量/编号等业务数字不报；但手机号、邮箱属于个人信息，要拦。
    "手机号": re.compile(r"(?<![\w(])1[3-9]\d{9}(?![\w)])"),
    "邮箱": re.compile(r"[\w.+-]+@[\w-]{2,}\.[\w.]{2,}"),
    "私钥": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "疑似凭据": re.compile(r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|password|passwd)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=]{12,}"),
    "本地路径": re.compile(r"[A-Za-z]:\\\\?Users\\\\?([^\\\\/\s\"'<>]+)"),
}

# 结构占位符：脱敏后的合法形态
PLACEHOLDER = [
    re.compile(r"^B0(?:ASIN\d{4,}|EXAMPLE\d*|XXXXXXXX)$"),
    re.compile(r"^ASIN_[A-Z0-9_]+$"),
]
STORE_PLACEHOLDER = re.compile(r"^(?:店铺[A-Z]{1,3}|BRAND_[A-Z]|[A-Za-z]{1,4})$")
PATH_USER_OK = {"user", "username", "you", "yourname", "[user]", "<user>", "{user}"}

# 明显是示例/文档数据的形态，不算命中
EXAMPLE_DOMAIN = re.compile(r"@(?:example\.(?:com|org|net)|test\.(?:com|org)|localhost)$", re.I)
STORE_DOC = re.compile(r"某|店铺[A-Z]|BRAND_")

# 命中值默认打码的类型（避免把 PII 再写进 CI 日志/工单）
MASK_KINDS = {"手机号", "邮箱", "疑似凭据", "私钥"}

OLE_MAGIC = b"\xd0\xcf\x11\xe0"


def _load(name, envkey):
    p = os.environ.get(envkey) or os.path.join(os.path.expanduser("~"), ".git-guard", name)
    if not os.path.isfile(p):
        return [], None
    out = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            s = line.rstrip("\n").strip()
            if s and not s.startswith("#"):
                out.append(s)
    return out, p


def decode_all(data):
    """返回多种候选解码文本。

    旧版只按 UTF-8 解码，于是三类文件整段失明：
      - UTF-16LE（PowerShell 5.1 的 > / Out-File / Export-Csv 默认）
      - 旧式 .xls/.doc（OLE 复合文档，文本是 UTF-16）
      - GBK/GB18030（中文 Excel「另存为 CSV」默认）

    反过来也不能无脑把所有编码都扫一遍：把一份 UTF-8 中文按 UTF-16LE 再解一次，
    乱码里很容易凑出 <xx>@<yy>.<zz> 这种形状，于是干净文档开始误报。
    所以先按证据判定编码，只在「本来就不是 UTF-8」时才取多编码并集。
    """
    # OOXML（docx/xlsx/pptx…）：包里的 xml 一定是 UTF-8，直接解包
    if data[:2] == b"PK":
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
            parts = [n for n in z.namelist() if n.endswith((".xml", ".rels", ".txt"))]
            if parts:
                return [b"\n".join(z.read(n) for n in parts).decode("utf-8", "ignore")]
        except Exception:
            pass

    # 有 BOM：编码是明确的
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return [data.decode("utf-16", "ignore")]
    if data[:3] == b"\xef\xbb\xbf":
        return [data[3:].decode("utf-8", "ignore")]

    # OLE 复合文档（.xls/.doc/.ppt）：BIFF8 的字符串有 UTF-16 也有 8 位压缩形态，
    # 所以 UTF-16 两种对齐 + latin-1（看 8 位串和裸 ASCII）都要扫。
    if data[:4] == OLE_MAGIC:
        return [data[8:].decode("utf-16-le", "ignore"),
                data[9:].decode("utf-16-le", "ignore"),
                data.decode("latin-1")]

    # 无 BOM 的 UTF-16：ASCII 部分每个字符都带一个 0 字节
    head = data[:8192]
    if len(head) >= 8 and head.count(0) * 8 >= len(head):
        enc = "utf-16-le" if head[1::2].count(0) >= head[0::2].count(0) else "utf-16-be"
        # 但「零字节多」也可能是稀疏文件 / 补齐过的二进制，
        # 里面往往夹着正常的 ASCII 文本。只看 UTF-16 会把它们整段解成乱码，
        # 于是 latin-1 这一路必须一起走。
        return [data.decode(enc, "ignore"), data.decode("latin-1")]

    try:
        return [data.decode("utf-8")]
    except UnicodeDecodeError:
        # 到这里才是不合法的 UTF-8：老 CSV（GBK）、错位 UTF-16、混合二进制，
        # 判定不了就都扫，宁可多一次正则。
        out = []
        for enc in ("utf-8", "gb18030", "utf-16-le"):
            try:
                out.append(data.decode(enc, "ignore"))
            except Exception:
                pass
        return out or [data.decode("latin-1")]


def scan_text(text, where, terms, allowres, found):
    def ok(kind, val):
        if kind == "ASIN" and any(p.match(val) for p in PLACEHOLDER):
            return False
        if kind == "店铺名" and (STORE_PLACEHOLDER.match(val) or STORE_DOC.search(val)):
            return False
        if kind == "邮箱" and EXAMPLE_DOMAIN.search(val):
            return False
        if kind == "本地路径" and val.lower() in PATH_USER_OK:
            return False
        for a in allowres:
            if a.search(val):
                return False
        return True

    for kind, rx in GENERIC.items():
        for m in rx.finditer(text):
            val = m.group(1) if kind == "本地路径" else m.group(0)
            if ok(kind, val):
                found.setdefault(kind, {}).setdefault(val, set()).add(where)
    for t in terms:
        # 键里不能拼词本身：那样 kind 标签会把要打码的明文又印出来
        if t in text and not any(a.search(t) for a in allowres):
            found.setdefault("自定义词", {}).setdefault(t, set()).add(where)


def scan_blob(data, where, terms, allowres, found, stats):
    if len(data) > MAX_BLOB_BYTES:
        # 头 + 尾，中间那段不看（并如实报出来）
        keep = max(MAX_BLOB_BYTES - TAIL_KEEP, TAIL_KEEP)
        data = data[:keep] + b"\n" + data[-TAIL_KEEP:]
        stats["truncated"].add(where)
    for text in decode_all(data):
        scan_text(text, where, terms, allowres, found)


def batch_blobs(shas):
    """一次 git cat-file --batch 取全部内容。

    旧版每个 blob 起一个子进程：802 个对象实测 118s，
    单进程批读 0.44s（同一台机器）。
    """
    if not shas:
        return
    # 分批：一次 --batch 会把整个范围的 blob 全读进内存，
    # 碰到几个大文件就是几百 MB 的峰值。按条数切块，峰值降到「一块的量」。
    CHUNK = 256
    for start in range(0, len(shas), CHUNK):
        chunk = shas[start:start + CHUNK]
        payload = ("\n".join(chunk) + "\n").encode()
        out = subprocess.run(["git", "cat-file", "--batch"], input=payload,
                             capture_output=True).stdout
        i, n = 0, len(out)
        while i < n:
            j = out.find(b"\n", i)
            if j < 0:
                break
            hdr = out[i:j].split()
            if len(hdr) < 3:            # missing / ambiguous
                i = j + 1
                continue
            sha, typ, size = hdr[0].decode(), hdr[1], int(hdr[2])
            body = out[j + 1:j + 1 + size]
            i = j + 1 + size + 1
            yield sha, typ, body


def staged_texts(stats):
    """git diff --cached --raw -z 直接给出新 blob sha。

    旧版用 --name-status -z 按两元组硬切，遇到 rename 会错位
    （R100 old new → 拆成 ('R100','old'), ('new','')），
    于是「git mv 同时改内容」这种最常见的操作，内容根本没被扫。
    """
    # 注意 --abbrev=40：--raw 默认给 7 位短 sha，与 cat-file 返回的完整 sha 对不上
    out = subprocess.run(["git", "diff", "--cached", "--raw", "-z",
                          "--no-renames", "--abbrev=40"],
                         capture_output=True).stdout.decode("utf-8", "ignore")
    items = out.split("\x00")
    jobs, i = [], 0
    while i + 1 < len(items):
        meta, path = items[i], items[i + 1]
        i += 2
        fields = meta.lstrip(":").split()
        if len(fields) < 5:
            continue
        new_mode, new_sha, status = fields[1], fields[3], fields[4]
        if new_mode == "160000":     # 子模块指针，没有内容可扫
            stats["skipped"].add(path)
            continue
        if status.startswith("D") or set(new_sha) == {"0"}:
            continue
        jobs.append((new_sha, path))
    paths = {}
    for sha, path in jobs:
        paths.setdefault(sha, set()).add(path)
    for sha, typ, body in batch_blobs([s for s, _ in jobs]):
        if typ != b"blob":
            continue
        # 同一份内容可能挂在多个路径上（复制出来的文件），全部报出来
        yield ", ".join(sorted(paths.get(sha, {sha}))), body


def _rev_args(range_lines):
    import shlex
    args = []
    for line in range_lines:
        args += shlex.split(line)
    return args


def range_texts(range_lines):
    """range_lines: 每行一个 rev-list 表达式（可含多个 token，如 'A..B' 或 'X --not --remotes'）"""
    args = ["git", "rev-list", "--objects"] + _rev_args(range_lines)
    out = subprocess.run(args, capture_output=True).stdout.decode("utf-8", "ignore")
    seen, jobs = set(), []
    for line in out.splitlines():
        parts = line.split(" ", 1)
        sha = parts[0]
        path = parts[1] if len(parts) > 1 else ""
        if sha in seen:
            continue
        seen.add(sha)
        jobs.append((sha, path))
    paths = {}
    for sha, path in jobs:
        paths.setdefault(sha, set()).add(path)
    for sha, typ, body in batch_blobs([s for s, _ in jobs]):
        if typ != b"blob":
            continue
        yield ", ".join(sorted(p for p in paths.get(sha, set()) if p) or {sha}), body


def range_messages(range_lines):
    """提交信息同样是泄露面：一条 -m "2.某店铺-DE 出货" 就已经进了历史。"""
    args = ["git", "log", "--format=%H%x00%B"] + _rev_args(range_lines)
    out = subprocess.run(args, capture_output=True).stdout.decode("utf-8", "ignore")
    for chunk in out.split("\x00"):
        # 输出是「sha\0消息\0sha\0消息…」，sha 自己不是消息，别当成一条来扫
        if chunk.strip() and not re.fullmatch(r"[0-9a-f]{40}", chunk.strip()):
            yield "<commit message>", chunk


def message_text(path):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return "<commit message>", f.read()


def mask(kind, val):
    if kind in MASK_KINDS or kind == "自定义词":
        if len(val) <= 4:
            return val[0] + "***"
        return val[:2] + "***" + val[-2:] + " (len=%d)" % len(val)
    return val


def _coverage_note(stats):
    """覆盖不足要一直说，包括被拦住的时候 —— 否则「拦了」会被误读成「扫全了」。"""
    out = []
    if stats and stats["skipped"]:
        out.append("  · 跳过 %d 个子模块指针（无内容可扫）" % len(stats["skipped"]))
    if stats and stats["truncated"]:
        out.append("  · %d 个对象超过 %d 字节，只扫了头尾（中间未扫）"
                   % (len(stats["truncated"]), MAX_BLOB_BYTES))
    return out


def report(found, extra="", show_values=False, stats=None):
    if not found:
        print("[git-guard] \u2713 \u672a\u53d1\u73b0\u654f\u611f\u4e1a\u52a1\u6570\u636e")
        for line in _coverage_note(stats):
            print(line)
        return 0
    print("")
    print("=" * 64)
    print("  git-guard\uff1a\u68c0\u6d4b\u5230\u7591\u4f3c\u654f\u611f\u4e1a\u52a1\u6570\u636e\uff0c\u5df2\u963b\u65ad")
    print("=" * 64)
    total = 0
    for kind in sorted(found):
        vals = found[kind]
        total += len(vals)
        items = list(vals.items())
        for v, ws in items[:6]:
            files = ", ".join(sorted(ws)[:2])
            shown = v if show_values else mask(kind, v)
            print("  [%s] %s   <- %s" % (kind, shown, files))
        if len(items) > 6:
            print("  [%s] ... \u53e6\u6709 %d \u4e2a" % (kind, len(items) - 6))
    for line in _coverage_note(stats):
        print(line)
    print("")
    print("  \u5171 %d \u4e2a\u4e0d\u540c\u503c\u547d\u4e2d\u3002%s" % (total, extra))
    print("")
    print("  \u5904\u7406\u65b9\u5f0f\uff1a")
    print("    1) \u6362\u6210\u5360\u4f4d\u7b26\uff1aB0ASIN0001 / \u5e97\u94faA-DE / PRICE_LOW / BRAND_A")
    print("    2) \u8be5\u503c\u5c5e\u6b63\u5e38\u4e1a\u52a1\u672f\u8bed -> \u5199\u5165 ~/.git-guard/allow.txt\uff08\u6b63\u5219\uff09")
    print("    3) \u786e\u8ba4\u8bef\u62a5 -> git commit/push --no-verify")
    if not show_values:
        print("")
        print("  \uff08\u654f\u611f\u503c\u5df2\u6253\u7801\uff1b\u770b\u5168\u6587\uff1apython scan.py --show-values ...\uff09")
    print("=" * 64)
    print("")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--message", metavar="FILE")
    ap.add_argument("--range", nargs="*", dest="ranges")
    ap.add_argument("--ranges-stdin", action="store_true")
    ap.add_argument("--no-messages", action="store_true", help="推送扫描时不看提交信息")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--show-values", action="store_true")
    ap.add_argument("--version", action="version", version="git-guard %s" % __version__)
    ap.add_argument("--repo", default=".")
    a = ap.parse_args()

    os.chdir(a.repo)
    terms, tpath = _load("terms.txt", "GIT_GUARD_TERMS")
    allowraw, apath = _load("allow.txt", "GIT_GUARD_ALLOW")
    allowres = []
    for r in allowraw:
        try:
            allowres.append(re.compile(r))
        except re.error:
            pass

    stats = {"skipped": set(), "truncated": set()}
    found = {}
    n = 0

    if a.staged:
        gen = staged_texts(stats)
    elif a.message:
        gen = [message_text(a.message)]
    elif a.ranges_stdin:
        lines = [l.rstrip("\n") for l in sys.stdin if l.strip()]
        if not lines:
            return 0
        gen = range_texts(lines)
        if not a.no_messages:
            gen = list(gen) + list(range_messages(lines))
    elif a.ranges:
        gen = range_texts(a.ranges)
    elif a.all:
        if subprocess.run(["git", "rev-parse", "--verify", "HEAD"],
                          capture_output=True).returncode != 0:
            return 0
        # 旧版这里是 ["HEAD"]：只扫 HEAD 可达对象，
        # 于是「清洗后复扫」会对 side 分支/已删分支上的旧 blob 报「干净」。
        gen = range_texts(["--all", "--reflog"])
    else:
        ap.error("需要 --staged / --message / --range / --ranges-stdin / --all")

    for where, data in gen:
        n += 1
        if isinstance(data, str):
            scan_text(data, where, terms, allowres, found)
        else:
            scan_blob(data, where, terms, allowres, found, stats)

    hint = "" if tpath else "（未找到词表 ~/.git-guard/terms.txt，仅通用模式）"
    return report(found, hint, a.show_values, stats)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
    except Exception as e:
        print("[git-guard] 运行错误: %s" % e, file=sys.stderr)
        sys.exit(2)
