#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git-guard scan — 扫描暂存区或待推送内容里的敏感业务数据。

用法:
  python scan.py --staged                  # pre-commit 调用
  python scan.py --range origin/main..HEAD # pre-push 调用（可多个 range）
  python scan.py --all                     # 全历史体检（只读，手动）

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

GENERIC = {
    "ASIN": re.compile(r"\bB0[A-Z0-9]{8}\b"),
    "店铺名": re.compile(r"\d+\.[\u4e00-\u9fa5]{2,}-(?:DE|FR|ES|IT|UK|US|MX|CA|JP|AU|AE|SA|NL|BE|SE|PL|IE)\b"),
    # 只报「百元以上」的金额：单价/费率是技能正文，营收/成本/发票才是敏感数据
    "金额(百元以上)": re.compile(r"(?:MX\$|€|£)\s?\d{3,}(?:[,.]\d{3})*(?:\.\d+)?"),
    "邮箱": re.compile(r"[\w.+-]+@[\w-]{2,}\.[\w.]{2,}"),
    "手机号": re.compile(r"(?<![\w(])1[3-9]\d{9}(?![\w)])"),
    "私钥": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "疑似凭据": re.compile(r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|password|passwd)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=]{12,}"),
    "本地路径": re.compile(r"[A-Za-z]:\\\\?Users\\\\?([^\\\\/\s\"'<>]+)"),
}

# 结构占位符：脱敏后的合法形态
PLACEHOLDER = [
    re.compile(r"^B0(?:ASIN\d{4}|EXAMPLE\d*|XXXXXXXX)$"),
    re.compile(r"^ASIN_[A-Z0-9_]+$"),
]
STORE_PLACEHOLDER = re.compile(r"^(?:店铺[A-Z]|BRAND_[A-Z]|[A-Za-z]{1,4})$")
PATH_USER_OK = {"user", "username", "you", "yourname", "[user]", "<user>", "{user}"}

# 明显是示例/文档数据的形态，不算命中
EXAMPLE_DOMAIN = re.compile(r"@(?:example\.(?:com|org|net)|test\.(?:com|org)|localhost)$", re.I)
STORE_DOC = re.compile(r"某|店铺[A-Z]|BRAND_")


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


def decode(data):
    text = data.decode("utf-8", "ignore")
    if data[:2] == b"PK":
        try:
            z = zipfile.ZipFile(io.BytesIO(data))
            parts = [n for n in z.namelist()
                     if n.endswith((".xml", ".rels", ".txt"))]
            if parts:
                return b"\n".join(z.read(n) for n in parts).decode("utf-8", "ignore")
        except Exception:
            pass
    return text


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
        if t in text and not any(a.search(t) for a in allowres):
            found.setdefault("自定义词:" + t, {}).setdefault(t, set()).add(where)


def staged_texts():
    out = subprocess.run(["git", "diff", "--cached", "--name-status", "-z"],
                         capture_output=True).stdout.decode("utf-8", "ignore")
    items = out.split("\x00")
    i = 0
    while i < len(items) - 1:
        status, path = items[i], items[i + 1]
        i += 2
        if status.startswith("D"):
            continue
        data = subprocess.run(["git", "show", ":" + path], capture_output=True).stdout
        yield path, decode(data)


def range_texts(range_lines):
    """range_lines: 每行一个 rev-list 表达式（可含多个 token，如 'A..B' 或 'X --not --remotes'）"""
    import shlex
    args = ["git", "rev-list", "--objects"]
    for line in range_lines:
        args += shlex.split(line)
    out = subprocess.run(args, capture_output=True).stdout.decode("utf-8", "ignore")
    seen = set()
    jobs = []
    for line in out.splitlines():
        parts = line.split(" ", 1)
        sha = parts[0]
        path = parts[1] if len(parts) > 1 else ""
        if sha in seen:
            continue
        seen.add(sha)
        jobs.append((sha, path))
    if not jobs:
        return
    # 批量取类型
    p = subprocess.run(["git", "cat-file", "--batch-check"],
                       input=("\n".join(s for s, _ in jobs)).encode(),
                       capture_output=True).stdout.decode("utf-8", "ignore")
    kinds = [l.split()[1] if len(l.split()) > 1 else "?" for l in p.splitlines()]
    for (sha, path), k in zip(jobs, kinds):
        if k != "blob":
            continue
        data = subprocess.run(["git", "cat-file", "-p", sha], capture_output=True).stdout
        yield (path or sha), decode(data)


def report(found, extra=""):
    if not found:
        print("[git-guard] \u2713 \u672a\u53d1\u73b0\u654f\u611f\u4e1a\u52a1\u6570\u636e")
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
            print("  [%s] %s   <- %s" % (kind, v, files))
        if len(items) > 6:
            print("  [%s] ... \u53e6\u6709 %d \u4e2a" % (kind, len(items) - 6))
    print("")
    print("  \u5171 %d \u4e2a\u4e0d\u540c\u503c\u547d\u4e2d\u3002%s" % (total, extra))
    print("")
    print("  \u5904\u7406\u65b9\u5f0f\uff1a")
    print("    1) \u6362\u6210\u5360\u4f4d\u7b26\uff1aB0ASIN0001 / \u5e97\u94faA-DE / PRICE_LOW / BRAND_A")
    print("    2) \u8be5\u503c\u5c5e\u6b63\u5e38\u4e1a\u52a1\u672f\u8bed -> \u5199\u5165 ~/.git-guard/allow.txt\uff08\u6b63\u5219\uff09")
    print("    3) \u786e\u8ba4\u8bef\u62a5 -> git commit/push --no-verify")
    print("=" * 64)
    print("")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--range", nargs="*", dest="ranges")
    ap.add_argument("--ranges-stdin", action="store_true")
    ap.add_argument("--all", action="store_true")
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

    if a.staged:
        gen = staged_texts()
    elif a.ranges_stdin:
        lines = [l.rstrip("\n") for l in sys.stdin if l.strip()]
        if not lines:
            return 0
        gen = range_texts(lines)
    elif a.ranges:
        gen = range_texts(a.ranges)
    elif a.all:
        if subprocess.run(["git", "rev-parse", "--verify", "HEAD"],
                          capture_output=True).returncode != 0:
            return 0
        gen = range_texts(["HEAD"])
    else:
        ap.error("需要 --staged / --range / --ranges-stdin / --all")

    found = {}
    n = 0
    for where, text in gen:
        n += 1
        scan_text(text, where, terms, allowres, found)

    hint = "" if tpath else "（未找到词表 ~/.git-guard/terms.txt，仅通用模式）"
    return report(found, hint)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
    except Exception as e:
        print("[git-guard] 运行错误: %s" % e, file=sys.stderr)
        sys.exit(2)
