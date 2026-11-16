#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git-guard scan — 扫描暂存区、待推提交或全历史里的敏感业务数据。

用法:
  python scan.py --staged                  # pre-commit 调用
  python scan.py --message FILE            # commit-msg 调用（扫提交信息）
  python scan.py --commit REV              # post-commit 复核（内容 + 信息）
  python scan.py --range origin/main..HEAD # pre-push 调用（可多个 range）
  python scan.py --ranges-stdin            # pre-push 调用（含提交信息）
  python scan.py --server-stdin            # pre-receive 调用（<old> <new> <ref>）
  python scan.py --all                     # 全历史体检（--all + --reflog）
  python scan.py --all --write-baseline    # 把存量命中记成基线，之后只拦新增

退出码: 0 干净（或只有基线内的存量命中） / 1 命中 / 2 运行错误

配置来源（后者覆盖前者）:
  ~/.git-guard/terms.txt        全局业务词，每行一个
  ~/.git-guard/allow.txt        全局放行正则，每行一个
  <repo>/.git-guard.conf        可入库的仓库配置（不含业务词）
  <repo>/.git/guard.conf        本地仓库配置（可含业务词，永不入库）
环境变量可改用 GIT_GUARD_TERMS / GIT_GUARD_ALLOW 指定路径。

配置行格式（`键: 值`，一行一条，`#` 开头是注释）:
  term: 某品牌名            业务敏感词（只放本地那一份）
  allow: ^B0EXAMPLE\\d*$    放行正则（作用于命中值）
  path-allow: vendor/**     不扫的路径（glob）
  disable: 手机号           关掉某一类检查
  max-blob: 32m             大对象上限
  baseline: .git/guard-baseline.txt
"""
import argparse
import fnmatch
import hashlib
import io
import os
import re
import subprocess
import sys
import zipfile

__version__ = "0.3.1"

# 单个对象整份解码的上限；更大的对象改走「可打印串抽取」+ 头尾解码
DEFAULT_MAX_BLOB = 32 * 1024 * 1024
TAIL_KEEP = 2 * 1024 * 1024
# 超过这个大小就不整份解码了：整份解 100MB 文本要十几秒，抽取只要零点几秒
LARGE_BLOB = 2 * 1024 * 1024
# 可打印串抽取：ASCII 段、以及 UTF-16 里的 ASCII 段（大二进制里绝大多数是这种）
ASCII_RUN = re.compile(rb"[\x20-\x7e]{6,}")
U16LE_RUN = re.compile(rb"(?:[\x20-\x7e]\x00){6,}")
U16BE_RUN = re.compile(rb"(?:\x00[\x20-\x7e]){6,}")

CONFIG_KEYS = ("term", "allow", "path-allow", "disable", "max-blob", "baseline")

GENERIC = {
    "ASIN": re.compile(r"\bB0[A-Z0-9]{8}\b"),
    "店铺名": re.compile(r"\d+\.[\u4e00-\u9fa5]{2,}-(?:DE|FR|ES|IT|UK|US|MX|CA|JP|AU|AE|SA|NL|BE|SE|PL|IE)\b"),
    # 金额/销量/编号等业务数字不报；但手机号、邮箱属于个人信息，要拦。
    "手机号": re.compile(r"(?<![\w(])1[3-9]\d{9}(?![\w)])"),
    "邮箱": re.compile(r"[\w.+-]+@[\w-]{2,}\.[\w.]{2,}"),
    "私钥": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "疑似凭据": re.compile(
        r"(?i)"
        r"(?:\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|password|passwd)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=]{12,})"
        r"|(?:\bAKIA[0-9A-Z]{16}\b)"                     # AWS access key id
        r"|(?:\bgh[pousr]_[A-Za-z0-9]{36,}\b)"           # GitHub token
        r"|(?:\bsk-[A-Za-z0-9]{20,}\b)"                  # OpenAI 风格
        r"|(?:\bxox[abposr]-[A-Za-z0-9-]{10,}\b)"        # Slack
        r"|(?:\bAIza[0-9A-Za-z_\-]{35}\b)"               # Google API key
        r"|(?:\bAtzr\|[A-Za-z0-9_\-]{20,}\b)"            # Amazon SP-API refresh token
        r"|(?:\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b)"  # JWT
        r"|(?:[a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s@/]+@)"  # URL 里带用户名密码
    ),
    "本地路径": re.compile(r"[A-Za-z]:\\\\?Users\\\\?([^\\\\/\s\"'<>]+)"),
    # 下面两类要算校验位，正则只做初筛，ok() 里再验证
    "身份证": re.compile(r"(?<![0-9A-Za-z])[1-9]\d{16}[\dXx](?![0-9A-Za-z])"),
    "银行卡": re.compile(r"(?<![\d.])\d{16,19}(?![\d.])"),
    # Seller ID 只在有标签时才算，否则 A+12~14 位太容易撞
    "Seller ID": re.compile(
        r"(?i)(?:seller[ _-]?id|merchant[ _-]?(?:id|token))['\"]?\s*[:=]\s*['\"]?"
        r"(A[A-Z0-9]{12,14})\b"),
}
# 带标签才有意义的那一类，取值用分组
GROUPED = {"本地路径": 1, "Seller ID": 1}

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

# Office 文档元数据里的默认值，报出来纯属噪音
OFFICE_DEFAULTS = {
    "", "microsoft office user", "author", "administrator", "admin",
    "user", "windows 用户", "unknown", "libreoffice", "wps",
}

# 子模块地址里明显指向内网的形态
INTERNAL_URL = re.compile(
    r"(?i)(?:localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+"
    r"|[A-Za-z0-9_-]+\.(?:internal|local|corp|lan)\b)")

# 命中值默认打码的类型（避免把 PII 再写进 CI 日志/工单）
MASK_KINDS = {"手机号", "邮箱", "疑似凭据", "私钥", "身份证", "银行卡",
              "Office元数据", "Seller ID"}

OLE_MAGIC = b"\xd0\xcf\x11\xe0"

# 身份证校验位
_ID_W = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_C = "10X98765432"


def _id18_ok(v):
    if len(v) != 18:
        return False
    if not v[:17].isdigit():
        return False
    s = sum(int(c) * w for c, w in zip(v[:17], _ID_W))
    return _ID_C[s % 11] == v[17].upper()


def _luhn_ok(v):
    total, alt = 0, False
    for ch in reversed(v):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


# ---------------------------------------------------------------- 配置

class Config(object):
    def __init__(self):
        self.terms = []
        self.allow = []
        self.path_allow = []
        self.disable = set()
        self.max_blob = int(os.environ.get("GIT_GUARD_MAX_BLOB", DEFAULT_MAX_BLOB))
        self.baseline_path = None
        self.sources = []

    def allowres(self):
        out = []
        for r in self.allow:
            try:
                out.append(re.compile(r))
            except re.error:
                pass
        return out


def _parse_size(v):
    m = re.match(r"(?i)^\s*(\d+)\s*([kmg]?)b?\s*$", v or "")
    if not m:
        return None
    n = int(m.group(1))
    return n * {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2).lower()]


def _read_lines(path):
    if not path or not os.path.isfile(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.rstrip("\n").strip()
            if s and not s.startswith("#"):
                out.append(s)
    return out


def _apply_config(cfg, path, default_kind=None):
    """读一份 `键: 值` 配置；也兼容词表/放行表那种「每行一个值」的老格式。

    default_kind 决定「不像配置的裸行」怎么处理：词表文件按 term、放行表按 allow，
    仓库配置文件则当噪音报出来（免得写错了还静默忽略）。
    """
    if not os.path.isfile(path):
        return False
    plain = []
    for line in _read_lines(path):
        m = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.+)$", line)
        if m and m.group(1).lower() in CONFIG_KEYS:
            k, v = m.group(1).lower(), m.group(2).strip()
            if k == "term":
                cfg.terms.append(v)
            elif k == "allow":
                cfg.allow.append(v)
            elif k == "path-allow":
                cfg.path_allow.append(v)
            elif k == "disable":
                cfg.disable.add(v)
            elif k == "max-blob":
                n = _parse_size(v)
                if n:
                    cfg.max_blob = n
            elif k == "baseline":
                cfg.baseline_path = v
        else:
            plain.append(line)
    for s in plain:
        if default_kind == "term":
            cfg.terms.append(s)
        elif default_kind == "allow":
            cfg.allow.append(s)
        else:
            print("[git-guard] %s: 无法识别的行，已忽略：%s" % (path, s[:60]),
                  file=sys.stderr)
    cfg.sources.append(path)
    return True


def load_config(repo, env=None):
    env = env or os.environ
    cfg = Config()
    home = os.path.join(os.path.expanduser("~"), ".git-guard")
    for name, key, kind in (("terms.txt", "GIT_GUARD_TERMS", "term"),
                            ("allow.txt", "GIT_GUARD_ALLOW", "allow")):
        _apply_config(cfg, env.get(key) or os.path.join(home, name), kind)
    # 仓库级：可入库的那份 + 本地那份
    root_conf = os.path.join(repo, ".git-guard.conf")
    _apply_config(cfg, root_conf)
    if os.path.isfile(root_conf) and "term:" in open(
            root_conf, encoding="utf-8", errors="replace").read():
        warn_msg = ("[git-guard] ⚠ %s 里有 term: —— 业务词不要放进会被提交的文件，"
                    "挪到 .git/guard.conf" % root_conf)
        print(warn_msg, file=sys.stderr)
    _apply_config(cfg, os.path.join(repo, ".git", "guard.conf"))
    if not cfg.baseline_path:
        cfg.baseline_path = os.path.join(repo, ".git", "guard-baseline.txt")
    return cfg


def path_allowed(cfg, path):
    for pat in cfg.path_allow:
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(path, pat.rstrip("/*")):
            return True
        if pat.endswith("/**") and path.startswith(pat[:-3]):
            return True
    return False


# ---------------------------------------------------------------- 基线

def _bkey(kind, val):
    return "%s\t%s" % (kind, hashlib.sha1(val.encode("utf-8")).hexdigest())


def load_baseline(path):
    keys = set()
    for line in _read_lines(path):
        if "\t" in line:
            keys.add(line)
    return keys


def write_baseline(path, found):
    lines = ["# git-guard 存量基线：这里只有哈希，没有明文。",
             "# 生成： scan.py --all --write-baseline",
             "# 目的：老仓库里的历史遗留不再阻断推送，只提示；新增值照拦。"]
    for kind in sorted(found):
        for val in sorted(found[kind]):
            lines.append(_bkey(kind, val))
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return len(lines) - 3


# ---------------------------------------------------------------- 解码

def _runs(data):
    """大对象：只抽可打印串，不解整份。

    100MB 的补齐文件整份解三遍要十几秒，抽 ASCII/UTF-16 段只要零点几秒，
    而且能覆盖到「中间段」——那正是头尾截断方案漏掉的地方。
    """
    out = []
    ascii_runs = [m.group(0) for m in ASCII_RUN.finditer(data)]
    if ascii_runs:
        out.append(b"\n".join(ascii_runs).decode("ascii", "ignore"))
    for rx, enc in ((U16LE_RUN, "utf-16-le"), (U16BE_RUN, "utf-16-be")):
        runs = [m.group(0).decode(enc, "ignore") for m in rx.finditer(data)]
        if runs:
            out.append("\n".join(runs))
    return out


def decode_all(data):
    """返回多种候选解码文本。

    只按 UTF-8 解码会漏掉三类（都是 Windows 上常见来源）：
      - UTF-16LE：PowerShell 5.1 的 > / Out-File / Export-Csv 默认
      - OLE 复合文档：旧式 .xls / .doc
      - GBK/GB18030：中文 Excel「另存为 CSV」默认

    反过来也不能无脑把所有编码都扫一遍：把一份 UTF-8 中文按 UTF-16LE 再解一次，
    乱码里很容易凑出 <xx>@<yy>.<zz> 这种形状，于是干净文档开始误报。
    所以先按证据判定编码，只在「本来就不是 UTF-8」时才取多编码并集。
    """
    texts = []
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
    # 所以 UTF-16 各对齐 + latin-1（看 8 位串和裸 ASCII）都要扫。
    if data[:4] == OLE_MAGIC:
        out = [data[i:].decode("utf-16-le", "ignore") for i in (0, 1, 8, 9)]
        out.append(data.decode("latin-1"))
        if len(data) > LARGE_BLOB:
            out += _runs(data)
        return out

    # 无 BOM 的 UTF-16：ASCII 部分每个字符都带一个 0 字节
    head = data[:8192]
    if len(head) >= 8 and head.count(0) * 8 >= len(head):
        le = head[1::2].count(0) >= head[0::2].count(0)
        enc = "utf-16-le" if le else "utf-16-be"
        # 但「零字节多」也可能是稀疏文件 / 补齐过的二进制，
        # 里面往往夹着正常的 ASCII 文本，所以 latin-1 这一路必须一起走。
        out = [data.decode(enc, "ignore"), data.decode("latin-1")]
        if len(data) > LARGE_BLOB:
            out += _runs(data)
        return out

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
        if len(data) > LARGE_BLOB:
            out += _runs(data)
        return out or [data.decode("latin-1")]


# ---------------------------------------------------------------- 检测

def _office_meta(data):
    """docx/xlsx 的作者、最后修改人、公司名 —— 最常见的隐性泄露。"""
    out = []
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        return out
    for name, tags in (("docProps/core.xml", ("dc:creator", "cp:lastModifiedBy")),
                       ("docProps/app.xml", ("Company",))):
        try:
            xml = z.read(name).decode("utf-8", "ignore")
        except Exception:
            continue
        for tag in tags:
            for m in re.finditer(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), xml, re.S):
                v = m.group(1).strip()
                if v and v.lower() not in OFFICE_DEFAULTS:
                    out.append(v)
    return out


def _submodule_urls(data):
    urls = []
    text = data.decode("utf-8", "ignore")
    for m in re.finditer(r"(?m)^\s*url\s*=\s*(\S+)", text):
        urls.append(m.group(1))
    return urls


def scan_text(text, where, cfg, allowres, found, baseline=None, known=None):
    def ok(kind, val):
        if kind == "ASIN" and any(p.match(val) for p in PLACEHOLDER):
            return False
        if kind == "店铺名" and (STORE_PLACEHOLDER.match(val) or STORE_DOC.search(val)):
            return False
        if kind == "邮箱" and EXAMPLE_DOMAIN.search(val):
            return False
        if kind == "本地路径" and val.lower() in PATH_USER_OK:
            return False
        if kind == "身份证" and not _id18_ok(val):
            return False
        if kind == "银行卡" and not _luhn_ok(val):
            return False
        for a in allowres:
            if a.search(val):
                return False
        return True

    def add(kind, val):
        if kind in cfg.disable:
            return
        if baseline is not None and _bkey(kind, val) in baseline:
            if known is not None:
                known.setdefault(kind, {}).setdefault(val, set()).add(where)
            return
        found.setdefault(kind, {}).setdefault(val, set()).add(where)

    for kind, rx in GENERIC.items():
        if kind in cfg.disable:
            continue
        for m in rx.finditer(text):
            idx = GROUPED.get(kind, 0)
            val = m.group(idx) if idx else m.group(0)
            if ok(kind, val):
                add(kind, val)
    for t in cfg.terms:
        # 键里不能拼词本身：那样 kind 标签会把要打码的明文又印出来
        if t in text and not any(a.search(t) for a in allowres):
            add("自定义词", t)


def scan_blob(data, where, cfg, allowres, found, stats, baseline=None, known=None):
    if len(data) > cfg.max_blob:
        keep = max(cfg.max_blob - TAIL_KEEP, TAIL_KEEP)
        texts = decode_all(data[:keep]) + decode_all(data[-TAIL_KEEP:]) + _runs(data)
        stats["truncated"].add(where)
    elif len(data) > LARGE_BLOB:
        # 大对象整份解码代价高，走抽取；头尾仍整份解一遍保准确率
        texts = decode_all(data[:LARGE_BLOB]) + decode_all(data[-LARGE_BLOB // 2:]) + _runs(data)
    else:
        texts = decode_all(data)

    for text in texts:
        scan_text(text, where, cfg, allowres, found, baseline, known)

    # 元数据类：只看特定结构，不走通用正则
    if data[:2] == b"PK" and "Office元数据" not in cfg.disable:
        for v in _office_meta(data):
            if baseline is not None and _bkey("Office元数据", v) in baseline:
                known.setdefault("Office元数据", {}).setdefault(v, set()).add(where)
            else:
                found.setdefault("Office元数据", {}).setdefault(v, set()).add(where)
    if where.endswith(".gitmodules") and "子模块地址" not in cfg.disable:
        for u in _submodule_urls(data):
            looks_internal = bool(INTERNAL_URL.search(u))
            has_cred = re.search(r"://[^\s:/@]+:[^\s@/]+@", u) is not None
            if looks_internal or has_cred:
                found.setdefault("子模块地址", {}).setdefault(u, set()).add(where)


# ---------------------------------------------------------------- git 读对象

def batch_blobs(shas):
    """一次 git cat-file --batch 取一批内容。

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


def staged_texts(cfg, stats):
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
        if path_allowed(cfg, path):
            stats["path_skipped"].add(path)
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


def range_texts(range_lines, cfg=None, stats=None):
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
    paths, allowed = {}, []
    for sha, path in jobs:
        paths.setdefault(sha, set()).add(path)
        if cfg is not None and path and path_allowed(cfg, path):
            if stats is not None:
                stats["path_skipped"].add(path)
            continue
        allowed.append(sha)
    for sha, typ, body in batch_blobs(allowed):
        if typ != b"blob":
            continue
        where = ", ".join(sorted(p for p in paths.get(sha, set()) if p) or {sha})
        yield where, body


def range_messages(range_lines):
    """提交信息同样是泄露面：一条 -m "2.某店铺-DE 出货" 就已经进了历史。"""
    args = ["git", "log", "--format=%H%x00%B"] + _rev_args(range_lines)
    out = subprocess.run(args, capture_output=True).stdout.decode("utf-8", "ignore")
    for chunk in out.split("\x00"):
        # 输出是「sha\0消息\0sha\0消息…」，sha 自己不是消息，别当成一条来扫
        if chunk.strip() and not re.fullmatch(r"[0-9a-f]{40}", chunk.strip()):
            yield "<commit message>", chunk


def commit_texts(rev):
    """单个提交：树里的 blob + 提交信息（post-commit 复核用）。"""
    ls = subprocess.run(["git", "ls-tree", "-r", "-z", "--full-tree", rev],
                        capture_output=True).stdout.decode("utf-8", "ignore")
    jobs, i = [], 0
    items = ls.split("\x00")
    while i < len(items):
        meta = items[i]
        i += 1
        if not meta.strip():
            continue
        fields = meta.split(None, 3)
        if len(fields) < 4:
            continue
        mode, _typ, sha, path = fields[0], fields[1], fields[2], fields[3]
        if mode == "160000":
            continue
        jobs.append((sha, path))
    paths = {}
    for sha, path in jobs:
        paths.setdefault(sha, set()).add(path)
    for sha, typ, body in batch_blobs([s for s, _ in jobs]):
        if typ != b"blob":
            continue
        yield ", ".join(sorted(paths.get(sha, {sha}))), body
    msg = subprocess.run(["git", "log", "-1", "--format=%B", rev],
                         capture_output=True).stdout.decode("utf-8", "ignore")
    if msg.strip():
        yield "<commit message>", msg


def message_text(path):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return "<commit message>", f.read()


# ---------------------------------------------------------------- 输出

def mask(kind, val):
    if kind in MASK_KINDS or kind == "自定义词":
        if len(val) <= 4:
            return val[0] + "***"
        return val[:2] + "***" + val[-2:] + " (len=%d)" % len(val)
    return val


def _coverage_note(stats, cfg):
    """覆盖不足要一直说，包括被拦住的时候 —— 否则「拦了」会被误读成「扫全了」。"""
    out = []
    if stats and stats.get("skipped"):
        out.append("  · 跳过 %d 个子模块指针（无内容可扫）" % len(stats["skipped"]))
    if stats and stats.get("path_skipped"):
        out.append("  · 按 path-allow 跳过 %d 个路径" % len(stats["path_skipped"]))
    if stats and stats.get("truncated"):
        out.append("  · %d 个对象超过 %d 字节，中间段只做了可打印串抽取"
                   % (len(stats["truncated"]), cfg.max_blob))
    if cfg and cfg.disable:
        out.append("  · 已按配置关掉：%s" % ", ".join(sorted(cfg.disable)))
    return out


def report(found, extra="", show_values=False, stats=None, cfg=None, known=None):
    known = known or {}
    for line in _coverage_note(stats, cfg):
        print(line)
    if known:
        tot = sum(len(v) for v in known.values())
        print("[git-guard] · 另有 %d 个值在基线里（存量，不阻断）：%s"
              % (tot, ", ".join("%s×%d" % (k, len(known[k])) for k in sorted(known))))
    if not found:
        print("[git-guard] \u2713 \u672a\u53d1\u73b0\u65b0\u7684\u654f\u611f\u4e1a\u52a1\u6570\u636e")
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
    print("")
    print("  \u5171 %d \u4e2a\u4e0d\u540c\u503c\u547d\u4e2d\u3002%s" % (total, extra))
    print("")
    print("  \u5904\u7406\u65b9\u5f0f\uff1a")
    print("    1) \u6362\u6210\u5360\u4f4d\u7b26\uff1aB0ASIN0001 / \u5e97\u94faA-DE / PHONE_C1 / BRAND_A")
    print("    2) \u8be5\u503c\u5c5e\u6b63\u5e38\u4e1a\u52a1\u672f\u8bed -> allow: <正则> 写进 .git/guard.conf")
    print("    3) \u5386\u53f2\u9057\u7559 -> scan.py --all --write-baseline\uff08\u53ea\u63d0\u793a\u4e0d\u963b\u65ad\uff09")
    print("    4) \u786e\u8ba4\u8bef\u62a5 -> git commit/push --no-verify")
    if not show_values:
        print("")
        print("  \uff08\u654f\u611f\u503c\u5df2\u6253\u7801\uff1b\u770b\u5168\u6587\uff1apython scan.py --show-values ...\uff09")
    print("=" * 64)
    print("")
    return 1


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--message", metavar="FILE")
    ap.add_argument("--commit", metavar="REV")
    ap.add_argument("--range", nargs="*", dest="ranges")
    ap.add_argument("--ranges-stdin", action="store_true")
    ap.add_argument("--server-stdin", action="store_true",
                    help="pre-receive：stdin 每行 <old> <new> <ref>")
    ap.add_argument("--ref", action="append", default=[], metavar="NAME",
                    help="另外扫一个 ref 名（分支名/tag 名也会泄露）")
    ap.add_argument("--no-messages", action="store_true", help="推送扫描时不看提交信息")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--no-baseline", action="store_true")
    ap.add_argument("--show-values", action="store_true")
    ap.add_argument("--version", action="version", version="git-guard %s" % __version__)
    ap.add_argument("--repo", default=".")
    a = ap.parse_args()

    os.chdir(a.repo)
    cfg = load_config(os.path.abspath(a.repo))
    allowres = cfg.allowres()

    baseline = None
    if not a.no_baseline and not a.write_baseline:
        baseline = load_baseline(cfg.baseline_path)

    stats = {"skipped": set(), "truncated": set(), "path_skipped": set()}

    if a.staged:
        gen = staged_texts(cfg, stats)
    elif a.message:
        gen = [message_text(a.message)]
    elif a.commit:
        gen = commit_texts(a.commit)
    elif a.server_stdin:
        lines = [l.split() for l in sys.stdin if l.strip()]
        ranges, refs = [], []
        for parts in lines:
            if len(parts) < 3:
                continue
            old, new, ref = parts[0], parts[1], parts[2]
            refs.append(ref)
            if set(new) == {"0"}:
                continue
            if set(old) == {"0"}:
                ranges.append("%s --not --all" % new)
            else:
                ranges.append("%s..%s" % (old, new))
        if not ranges and not refs:
            return 0
        gen = range_texts(ranges) if ranges else []
        gen = list(gen) + (list(range_messages(ranges)) if ranges and not a.no_messages else [])
        for r_ in refs:
            gen.append(("<ref>", re.sub(r"^refs/(heads|tags)/", "", r_)))
    elif a.ranges_stdin:
        lines = [l.rstrip("\n") for l in sys.stdin if l.strip()]
        if not lines:
            return 0
        gen = range_texts(lines, cfg, None)
        if not a.no_messages:
            gen = list(gen) + list(range_messages(lines))
    elif a.ranges:
        gen = range_texts(a.ranges, cfg, stats)
    elif a.all:
        if subprocess.run(["git", "rev-parse", "--verify", "HEAD"],
                          capture_output=True).returncode != 0:
            return 0
        # 旧版这里是 ["HEAD"]：只扫 HEAD 可达对象，
        # 于是「清洗后复扫」会对 side 分支/已删分支上的旧 blob 报「干净」。
        gen = range_texts(["--all", "--reflog"], cfg, None)
    elif a.ref:
        # 只给了 ref 名：pre-push 在「没有新提交」时就只查名字
        gen = []
    else:
        ap.error("需要 --staged / --message / --commit / --range / --ranges-stdin / --server-stdin / --all / --ref")

    # ref 名单独给的时候（pre-push 传过来的分支名/tag 名）
    for name in a.ref:
        gen.append(("<ref>", re.sub(r"^refs/(heads|tags)/", "", name)))

    found, known = {}, {}
    for where, data in gen:
        if isinstance(data, str):
            scan_text(data, where, cfg, allowres, found, baseline, known)
        else:
            scan_blob(data, where, cfg, allowres, found, stats, baseline, known)

    if a.write_baseline:
        n = write_baseline(cfg.baseline_path, found)
        print("[git-guard] 已写入基线 %d 条 -> %s" % (n, cfg.baseline_path))
        print("  之后这些值只提示、不阻断；新增值照拦。删掉该文件即恢复全量阻断。")
        return 0

    hint = "" if cfg.terms else "（未找到业务词表，仅通用模式）"
    return report(found, hint, a.show_values, stats, cfg, known)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
    except Exception as e:
        print("[git-guard] 运行错误: %s" % e, file=sys.stderr)
        sys.exit(2)
