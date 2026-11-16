#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git-guard 落地自测：走真实 git 操作，用已装好的全局钩子。

    python install.py            # 先装（CI 里没有这一步，所以本文件不进 CI）
    python tests/live_hooks.py

和 run_tests.py 的区别：那边测扫描器本身（临时仓库 + 空词表 + 屏蔽全局钩子），
这边什么都不屏蔽 —— 普通用户日常那条路径：git init -> commit -> push，
看钩子到底拦没拦住、放行有没有放错。会临时写 ~/.git-guard/bypass.log，结束时还原。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

HOME = os.path.expanduser("~")
BYPASS = os.path.join(HOME, ".git-guard", "bypass.log")
TMP = tempfile.mkdtemp(prefix="guard-live-")
RESULTS = []

ASIN = "B0" + "ABCD1234"
PHONE = "138" + "12345678"
STORE = "2." + "甲乙丙" + "-DE"


def run(args, cwd, check=False, env_extra=None, input_bytes=None):
    env = os.environ.copy()
    env.pop("GIT_GUARD_HOME", None)          # 用真实的 ~/.git-guard
    env.pop("GIT_GUARD_TERMS", None)
    env.pop("GIT_GUARD_ALLOW", None)
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "live"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "live@example.com"
    env["GIT_TERMINAL_PROMPT"] = "0"
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(args, cwd=cwd, env=env, input=input_bytes,
                       capture_output=True, text=True, errors="replace")
    if check and r.returncode != 0:
        raise AssertionError("命令失败: %s\n%s\n%s" % (" ".join(args), r.stdout, r.stderr))
    return r


def out(r):
    return (r.stdout or "") + (r.stderr or "")


def case(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (("   " + detail) if detail and not ok else ""))
    if not ok and detail:
        print("      " + detail.replace("\n", "\n      ")[:1200])


def new_repo(name="r"):
    path = tempfile.mkdtemp(prefix=name + "-", dir=TMP)
    run(["git", "init", "-q", "-b", "main", "."], path, check=True)
    return path


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


# ---------------------------------------------------------------- 用例

def t01_hooks_installed_from_template():
    repo = new_repo()
    hooks = os.path.join(repo, ".git", "hooks")
    names = sorted(n for n in os.listdir(hooks) if not n.endswith(".sample"))
    want = ["commit-msg", "post-commit", "pre-commit", "pre-push"]
    case("01 git init 自动带上四个钩子", names == want, "实际: %s" % names)


def t02_pre_commit_blocks():
    repo = new_repo()
    write(os.path.join(repo, "a.txt"), "订单 %s 手机 %s\n" % (ASIN, PHONE))
    run(["git", "add", "-A"], repo)
    r = run(["git", "commit", "-m", "add data"], repo)
    ok = r.returncode != 0 and "阻断" in out(r) and "ASIN" in out(r)
    case("02 git commit 被 pre-commit 拦住", ok, out(r))


def t03_commit_msg_blocks():
    repo = new_repo()
    write(os.path.join(repo, "a.txt"), "# 干净的正文\n")
    run(["git", "add", "-A"], repo)
    r = run(["git", "commit", "-m", "出货 %s %s" % (STORE, PHONE)], repo)
    ok = r.returncode != 0 and "<commit message>" in out(r)
    case("03 提交信息被 commit-msg 拦住", ok, out(r))


def t04_clean_commit_passes():
    repo = new_repo()
    write(os.path.join(repo, "a.txt"), "占位符 B0ASIN0001 / 店铺A-DE / you@example.com\n")
    run(["git", "add", "-A"], repo)
    r = run(["git", "commit", "-m", "docs: 用占位符"], repo)
    case("04 占位符内容正常提交", r.returncode == 0, out(r))
    return repo


def t05_no_verify_leaves_a_trace():
    before = os.path.getsize(BYPASS) if os.path.isfile(BYPASS) else 0
    repo = new_repo()
    write(os.path.join(repo, "leak.txt"), ASIN + "\n")
    run(["git", "add", "-A"], repo)
    r = run(["git", "commit", "--no-verify", "-m", "bypass"], repo)
    time.sleep(0.3)
    after = os.path.getsize(BYPASS) if os.path.isfile(BYPASS) else 0
    ok = r.returncode == 0 and after > before
    case("05 --no-verify 绕过被 post-commit 记账", ok,
         "log %d -> %d 字节\n%s" % (before, after, out(r)))


def t06_pre_push_blocks_to_local_remote():
    remote = tempfile.mkdtemp(prefix="remote-", dir=TMP)
    run(["git", "init", "-q", "--bare", "."], remote, check=True)
    repo = new_repo()
    write(os.path.join(repo, "a.txt"), "# clean\n")
    run(["git", "add", "-A"], repo)
    run(["git", "commit", "-m", "init"], repo, check=True)
    run(["git", "remote", "add", "origin", remote], repo, check=True)
    write(os.path.join(repo, "leak.txt"), PHONE + "\n")
    run(["git", "add", "-A"], repo)
    run(["git", "commit", "--no-verify", "-m", "leak"], repo)
    r = run(["git", "push", "origin", "main"], repo)
    ok = r.returncode != 0 and "待推送" in out(r) and "手机号" in out(r)
    case("06 git push 被 pre-push 拦住", ok, out(r))
    # 真实远端里不该出现这个提交
    ls = run(["git", "log", "--oneline", "--all"], remote)
    case("06b 远端确实没收到那个提交", "leak" not in out(ls), out(ls))
    return remote, repo


def t07_ref_name_is_checked(remote, repo):
    run(["git", "checkout", "-q", "-b", STORE + "-fix"], repo, check=True)
    r = run(["git", "push", "origin", STORE + "-fix"], repo)
    ok = r.returncode != 0 and "<ref>" in out(r)
    case("07 分支名里的店铺名被拦", ok, out(r))
    run(["git", "checkout", "-q", "main"], repo)


def t08_baseline_lets_old_values_through():
    repo = new_repo()
    write(os.path.join(repo, "old.txt"), ASIN + "\n")
    run(["git", "add", "-A"], repo)
    run(["git", "commit", "--no-verify", "-m", "legacy"], repo, check=True)
    r = run(["python", os.path.join(HOME, ".git-guard", "scan.py"),
             "--all", "--write-baseline"], repo)
    ok1 = r.returncode == 0 and "写入基线" in out(r)
    write(os.path.join(repo, "again.txt"), ASIN + "\n")
    run(["git", "add", "-A"], repo)
    r2 = run(["git", "commit", "-m", "same value again"], repo)
    ok2 = r2.returncode == 0 and "基线" in out(r2)
    case("08 存量基线：老值放行、只提示", ok1 and ok2, out(r) + out(r2))
    write(os.path.join(repo, "new.txt"), "B0" + "WXYZ9876" + "\n")
    run(["git", "add", "-A"], repo)
    r3 = run(["git", "commit", "-m", "new value"], repo)
    case("08b 基线不影响新值：新值照拦", r3.returncode != 0, out(r3))


def t09_path_allow():
    repo = new_repo()
    write(os.path.join(repo, ".git-guard.conf"), "path-allow: vendor/**\n")
    write(os.path.join(repo, "vendor", "x.txt"), ASIN + "\n")
    run(["git", "add", "-A"], repo)
    r = run(["git", "commit", "-m", "vendor dump"], repo)
    case("09 path-allow 让 vendor/** 不参与扫描", r.returncode == 0, out(r))


def t10_disable_one_check():
    repo = new_repo()
    write(os.path.join(repo, ".git-guard.conf"), "disable: 手机号\n")
    write(os.path.join(repo, "p.txt"), PHONE + "\n")
    run(["git", "add", "-A"], repo)
    r = run(["git", "commit", "-m", "phone only"], repo)
    ok1 = r.returncode == 0
    write(os.path.join(repo, "b.txt"), ASIN + "\n")
    run(["git", "add", "-A"], repo)
    r2 = run(["git", "commit", "-m", "asin too"], repo)
    ok2 = r2.returncode != 0 and "ASIN" in out(r2)
    case("10 disable: 手机号 只关那一类", ok1 and ok2, out(r) + out(r2))


def t11_office_metadata():
    repo = new_repo()
    p = os.path.join(repo, "方案.docx")
    z = zipfile.ZipFile(p, "w")
    z.writestr("[Content_Types].xml", "<Types/>")
    z.writestr("word/document.xml", "<w/>")
    z.writestr("docProps/core.xml",
               "<cp:coreProperties><dc:creator>%s</dc:creator></cp:coreProperties>"
               % ("张" + "三"))
    z.close()
    run(["git", "add", "-A"], repo)
    r = run(["git", "commit", "-m", "add doc"], repo)
    ok = r.returncode != 0 and "Office元数据" in out(r)
    case("11 docx 里的作者真名被拦", ok, out(r))


def t12_encoding_paths():
    repo = new_repo()
    with open(os.path.join(repo, "ps.txt"), "wb") as f:
        f.write(b"\xff\xfe" + ("订单 %s\n" % ASIN).encode("utf-16-le"))
    run(["git", "add", "-A"], repo)
    r = run(["git", "commit", "-m", "powershell export"], repo)
    case("12 UTF-16 文件被拦（PowerShell 重定向那种）", r.returncode != 0, out(r))


def t13_server_side_rejects(remote, repo):
    src = os.environ.get("GUARD_SRC") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    server_dir = tempfile.mkdtemp(prefix="srv-", dir=TMP)
    shutil.copy(os.path.join(HOME, ".git-guard", "scan.py"), server_dir)
    hooks = os.path.join(remote, "hooks")
    os.makedirs(hooks, exist_ok=True)
    shutil.copy(os.path.join(src, "server", "pre-receive"),
                os.path.join(hooks, "pre-receive"))
    os.chmod(os.path.join(hooks, "pre-receive"), 0o755)
    write(os.path.join(repo, "srv-leak.txt"), PHONE + "\n")
    run(["git", "add", "-A"], repo)
    run(["git", "commit", "--no-verify", "-m", "server test"], repo, check=True)
    r = run(["git", "push", "--no-verify", "origin", "main"], repo,
            env_extra={"GIT_GUARD_HOME": server_dir})
    ok = r.returncode != 0 and "服务端拒绝" in out(r)
    case("13 服务端 pre-receive 拦住（客户端 --no-verify 无效）", ok, out(r))


def main():
    src = os.environ.get("GUARD_SRC", "")
    before = None
    if os.path.isfile(BYPASS):
        with open(BYPASS, "rb") as f:
            before = f.read()
    try:
        t01_hooks_installed_from_template()
        t02_pre_commit_blocks()
        t03_commit_msg_blocks()
        t04_clean_commit_passes()
        t05_no_verify_leaves_a_trace()
        remote, repo = t06_pre_push_blocks_to_local_remote()
        t07_ref_name_is_checked(remote, repo)
        t08_baseline_lets_old_values_through()
        t09_path_allow()
        t10_disable_one_check()
        t11_office_metadata()
        t12_encoding_paths()
        t13_server_side_rejects(remote, repo)
    finally:
        # 还原用户的 bypass.log，别把测试条目留在里面
        if before is not None:
            with open(BYPASS, "wb") as f:
                f.write(before)
        elif os.path.isfile(BYPASS):
            os.remove(BYPASS)
        shutil.rmtree(TMP, ignore_errors=True)

    bad = [r for r in RESULTS if not r[1]]
    print("\n%d/%d 通过" % (len(RESULTS) - len(bad), len(RESULTS)))
    if bad:
        print("失败: " + ", ".join(r[0] for r in bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
