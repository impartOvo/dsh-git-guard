#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git-guard 回归测试（只用标准库，不依赖 pytest）

    python tests/run_tests.py            # 全部用例
    python tests/run_tests.py -v         # 打印每个用例的扫描器输出

每个用例在临时目录里现建仓库，跑完删掉；不碰你的全局钩子、不碰你的词表：
  - GIT_GUARD_HOME 指向不存在的目录  -> 被测仓库里的 git commit 不会触发本机已装钩子
  - GIT_GUARD_TERMS / GIT_GUARD_ALLOW 指向空文件 -> 结果不随开发者的词表变化

用例里的真实值一律由 _s() 拼出来，源码里不存在完整串，
免得这个仓库自己被自己拦下来。
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCAN = os.path.join(ROOT, "scan.py")
SANITIZE = os.path.join(ROOT, "sanitize.py")
PY = sys.executable

TESTS = []


def _s(*parts):
    """拼出测试用的真实值，避免在源码里留下可被扫描的完整串。"""
    return "".join(parts)


ASIN = _s("B0", "ABCD1234")
ASIN2 = _s("B0", "WXYZ9876")
STORE_DE = _s("2.", "甲乙丙", "-DE")
STORE_DE2 = _s("5.", "丁戊己", "-DE")
STORE_FR = _s("9.", "庚辛壬", "-FR")
PHONE = _s("138", "1234", "5678")
MAIL = _s("someone", "@", "corp-mail", ".", "com")


def test(fn):
    TESTS.append(fn)
    return fn


# ---------------------------------------------------------------- 基础设施

def sh(args, cwd, env_extra=None, input_bytes=None):
    env = os.environ.copy()
    env.update({
        "GIT_GUARD_HOME": os.path.join(cwd, ".no-guard"),
        "GIT_GUARD_TERMS": os.path.join(cwd, ".empty-terms"),
        "GIT_GUARD_ALLOW": os.path.join(cwd, ".empty-allow"),
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.t",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    if env_extra:
        env.update(env_extra)
    return subprocess.run(args, cwd=cwd, env=env, input=input_bytes,
                          capture_output=True)


def new_repo(tmp, name=None):
    path = tempfile.mkdtemp(prefix=(name or "r") + "-", dir=tmp)
    for f in (".empty-terms", ".empty-allow"):
        open(os.path.join(path, f), "w").close()
    sh(["git", "init", "-q", "-b", "main", "."], path)
    sh(["git", "commit", "-q", "--allow-empty", "-m", "init"], path)
    return path


def scan(repo, *args, **kw):
    r = sh([PY, SCAN] + list(args), repo, **kw)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def write(path, text, encoding="utf-8"):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with io.open(path, "w", encoding=encoding, newline="") as f:
        f.write(text)


def write_bytes(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def add_commit(repo, path, message="c", no_verify=True):
    sh(["git", "add", "-A"], repo)
    args = ["git", "commit", "-q", "-m", message]
    if no_verify:
        args.insert(2, "--no-verify")
    r = sh(args, repo)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return r


# ---------------------------------------------------------------- 用例

@test
def staged_plain_leak_blocks(tmp):
    """暂存区里的 ASIN / 手机号要拦住（基线行为）"""
    repo = new_repo(tmp)
    write(os.path.join(repo, "a.txt"), "%s %s\n" % (ASIN, PHONE))
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 1, out
    assert "ASIN" in out and "手机号" in out, out
    return out


@test
def staged_clean_passes(tmp):
    """占位符 / example.com / C:\\Users\\user 都不该报（零误报基线）"""
    repo = new_repo(tmp)
    write(os.path.join(repo, "doc.md"),
          "示例：B0ASIN0001、B0EXAMPLE1、店铺A-DE、you@example.com、"
          "C:\\Users\\user\\x、PRICE_LOW\n")
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 0, out
    return out


@test
def utf16le_is_scanned(tmp):
    """UTF-16LE（PowerShell 5.1 重定向默认）过去整段失明"""
    repo = new_repo(tmp)
    body = "%s %s\n" % (ASIN, PHONE)
    write_bytes(os.path.join(repo, "v16.txt"),
                b"\xff\xfe" + body.encode("utf-16-le"))
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 1, "UTF-16LE 未被扫描：\n" + out
    assert "ASIN" in out, out
    return out


@test
def utf16le_pure_cjk_term_is_scanned(tmp):
    """无 BOM、纯中文的 UTF-16LE：既不是合法 UTF-8 也没有 0 字节，走并集分支"""
    repo = new_repo(tmp)
    word = _s("甲乙", "丙丁", "戊")
    write_bytes(os.path.join(repo, "v16cjk.txt"), word.encode("utf-16-le"))
    terms = os.path.join(repo, ".terms")
    write(terms, word + "\n")
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged", env_extra={"GIT_GUARD_TERMS": terms})
    assert rc == 1, "无 BOM 的 UTF-16LE 中文词未被扫描：\n" + out
    assert "自定义词" in out, out
    return out


@test
def legacy_xls_ole_is_scanned(tmp):
    """旧式 .xls（OLE 复合文档，正文 UTF-16）过去整段失明"""
    repo = new_repo(tmp)
    body = "%s %s" % (ASIN, PHONE)
    write_bytes(os.path.join(repo, "legacy.xls"),
                b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + body.encode("utf-16-le"))
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 1, "OLE 二进制未被扫描：\n" + out
    assert "手机号" in out, out
    return out


@test
def gbk_chinese_only_is_scanned(tmp):
    """GBK（中文 Excel 另存 CSV 默认）里的纯中文店铺名过去看不见"""
    repo = new_repo(tmp)
    write_bytes(os.path.join(repo, "gbk.csv"), (STORE_DE + "\n").encode("gb18030"))
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 1, "GBK 内容未被扫描：\n" + out
    assert "店铺名" in out, out
    return out


@test
def rename_with_same_content_is_scanned(tmp):
    """git mv（相似度够高、git 判定为 R）过去会整份漏掉，且报的路径要对"""
    repo = new_repo(tmp)
    filler = "".join("第 %d 行：课程正文，无标识\n" % i for i in range(1, 41))
    write(os.path.join(repo, "manual.txt"), filler + "末行 %s %s\n" % (ASIN, PHONE))
    add_commit(repo, "manual.txt", "legacy")
    sh(["git", "mv", "manual.txt", "handbook.txt"], repo)
    status = sh(["git", "diff", "--cached", "--name-status"], repo)
    assert b"R100" in status.stdout, status.stdout
    rc, out = scan(repo, "--staged")
    assert rc == 1, "rename（R100）内容未被扫描：\n" + out
    assert "handbook.txt" in out, out
    return out


@test
def rename_with_edit_is_scanned(tmp):
    """git mv 之后再改内容（相似度低，git 报 D+A）也要拦住"""
    repo = new_repo(tmp)
    write(os.path.join(repo, "rpt.txt"), "# report\n")
    add_commit(repo, "rpt.txt", "base")
    sh(["git", "mv", "rpt.txt", "rpt2.txt"], repo)
    write(os.path.join(repo, "rpt2.txt"), "# report\n%s %s\n" % (ASIN, PHONE))
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 1, out
    return out


@test
def all_flag_covers_other_branches(tmp):
    """--all 必须覆盖所有 ref：过去只扫 HEAD，对 side 分支报「干净」"""
    repo = new_repo(tmp)
    sh(["git", "checkout", "-q", "-b", "side"], repo)
    write(os.path.join(repo, "side.txt"), ASIN + "\n")
    add_commit(repo, "side.txt", "side leak")
    sh(["git", "checkout", "-q", "main"], repo)
    write(os.path.join(repo, "clean.txt"), "# clean\n")
    add_commit(repo, "clean.txt", "clean")
    rc, out = scan(repo, "--all")
    assert rc == 1, "--all 没看到 side 分支上的泄露：\n" + out
    return out


@test
def commit_message_is_scanned(tmp):
    """提交信息本身也是泄露面（过去完全没有入口检查）"""
    repo = new_repo(tmp)
    msg = os.path.join(repo, ".git", "COMMIT_EDITMSG")
    write(msg, "出货 %s %s\n" % (STORE_DE, PHONE))
    rc, out = scan(repo, "--message", msg)
    assert rc == 1, out
    assert "<commit message>" in out, out
    return out


@test
def range_scan_includes_messages(tmp):
    """pre-push 的 --ranges-stdin 必须连提交信息一起扫"""
    repo = new_repo(tmp)
    write(os.path.join(repo, "x.txt"), "# 无标识\n")
    sh(["git", "add", "-A"], repo)
    sh(["git", "commit", "-q", "--no-verify", "-m", "出货 " + PHONE], repo)
    rc, out = scan(repo, "--ranges-stdin", input_bytes=("HEAD\n").encode())
    assert rc == 1, "推送扫描没看到提交信息里的手机号：\n" + out
    return out


@test
def pii_is_masked_by_default(tmp):
    """默认输出要打码（否则 PII 又被抄进 CI 日志一份）"""
    repo = new_repo(tmp)
    write(os.path.join(repo, "a.txt"), PHONE + "\n")
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 1, out
    assert PHONE not in out, "手机号明文出现在输出里：\n" + out
    rc, out2 = scan(repo, "--staged", "--show-values")
    assert PHONE in out2, "--show-values 应给出明文：\n" + out2
    return out


@test
def allow_list_is_exact(tmp):
    """放行是精确匹配：放行这一个值，别的同类值仍要拦"""
    repo = new_repo(tmp)
    write(os.path.join(repo, ".allow"), "^%s$\n" % ASIN)
    write(os.path.join(repo, "a.txt"), "%s %s\n" % (ASIN, ASIN2))
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged",
                   env_extra={"GIT_GUARD_ALLOW": os.path.join(repo, ".allow")})
    assert rc == 1, out
    assert ASIN2 in out and ASIN not in out, out
    return out


@test
def suggest_collects_every_store(tmp):
    """suggest 必须采集同一站点的每个店铺，占位符从 A 起且不撞名"""
    repo = new_repo(tmp)
    write(os.path.join(repo, "a.txt"),
          "%s %s %s %s %s %s\n" % (STORE_DE, STORE_DE2, STORE_FR, ASIN, ASIN2, MAIL))
    add_commit(repo, "a.txt", "data")
    out_map = os.path.join(repo, "map.txt")
    r = sh([PY, SANITIZE, "suggest", "--output", out_map], repo)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    text = io.open(out_map, encoding="utf-8").read()
    shops = [l for l in text.splitlines() if l.endswith(("-%s" % "DE", "-FR")) and "==>" in l]
    assert STORE_DE2 in text, "同站点的第二个店铺丢了：\n" + text
    assert "店铺A-" in text, "占位符应从 A 起：\n" + text
    assert len(shops) == 3, text
    labels = [l.split("==>")[1] for l in shops]
    assert len(set(labels)) == 3, "占位符撞名：%r" % labels
    return text


@test
def fallback_regex_keeps_placeholders(tmp):
    """兜底正则不能把刚生成的占位符吃掉（filter-repo 先字面量、后正则）"""
    import ast
    tree = ast.parse(io.open(SANITIZE, encoding="utf-8").read())
    rule = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append" and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value.startswith("regex:")):
            rule = node.args[0].value
            break
    assert rule, "没在 sanitize.py 里找到兜底正则"
    pattern, _, repl = rule[len("regex:"):].partition("==>")
    fx = [ASIN, "B0ASIN0001", "B0EXAMPLE1", "B0XXXXXXXX"]
    got = [re.sub(pattern, repl, v) for v in fx]
    assert got == ["B0ASINREDACTED", "B0ASIN0001", "B0EXAMPLE1", "B0XXXXXXXX"], got
    return str(got)


@test
def full_history_is_fast(tmp):
    """批读：200 个对象不该再花几十秒（旧实现每对象一个子进程）"""
    repo = new_repo(tmp)
    for i in range(200):
        write(os.path.join(repo, "f%03d.txt" % i), "第 %d 行 数据\n" % i)
    add_commit(repo, None, "bulk")
    t = time.time()
    rc, out = scan(repo, "--all")
    d = time.time() - t
    assert rc == 0, out
    assert d < 20, "200 个对象扫了 %.1fs，批读没生效？" % d
    return "200 objects in %.2fs" % d


# ---------------------------------------------------------------- runner

def main(argv):
    verbose = "-v" in argv or "--verbose" in argv
    only = [a for a in argv[1:] if not a.startswith("-")]
    tmp = tempfile.mkdtemp(prefix="git-guard-tests-")
    fails = []
    try:
        for fn in TESTS:
            if only and not any(o in fn.__name__ for o in only):
                continue
            name = fn.__name__
            try:
                detail = fn(tmp)
                print("PASS  %s" % name)
                if verbose and detail:
                    print("      " + str(detail).replace("\n", "\n      ")[:2000])
            except AssertionError as e:
                fails.append(name)
                print("FAIL  %s" % name)
                print("      " + str(e).replace("\n", "\n      ")[:4000])
            except Exception as e:                       # noqa: BLE001
                fails.append(name)
                print("ERROR %s: %r" % (name, e))
            # 每个用例用独立临时目录，互不干扰
            for d in os.listdir(tmp):
                p = os.path.join(tmp, d)
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total = len([t for t in TESTS if not only or any(o in t.__name__ for o in only)])
    print("\n%d/%d passed" % (total - len(fails), total))
    if fails:
        print("failed: " + ", ".join(fails))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
