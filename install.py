#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安装 git-guard 钩子到全局 git 模板。

    python install.py            安装/升级
    python install.py --version  看版本
"""
import os
import shutil
import subprocess
import sys

__version__ = "0.3.0"

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD_HOME = os.path.join(os.path.expanduser("~"), ".git-guard")
TEMPLATE = os.path.join(os.path.expanduser("~"), ".git-templates")
HOOKS = os.path.join(TEMPLATE, "hooks")
HOOK_NAMES = ("pre-commit", "commit-msg", "pre-push", "post-commit")


def main():
    if "--version" in sys.argv[1:]:
        print("git-guard %s" % __version__)
        return 0
    print("安装 git-guard %s" % __version__)
    print("  脚本目录: " + HERE)

    os.makedirs(GUARD_HOME, exist_ok=True)
    os.makedirs(HOOKS, exist_ok=True)

    # 1) 扫描器
    for f in ("scan.py", "sanitize.py"):
        shutil.copy2(os.path.join(HERE, f), os.path.join(GUARD_HOME, f))
    print("  ✓ 扫描器 -> %s" % GUARD_HOME)

    # 2) 钩子
    for h in HOOK_NAMES:
        src = os.path.join(HERE, h)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(HOOKS, h)
        shutil.copy2(src, dst)
        try:
            os.chmod(dst, 0o755)
        except Exception:
            pass
        print("  ✓ 钩子 -> %s" % dst)

    # 3) 全局模板
    old = subprocess.run(["git", "config", "--global", "--get", "init.templateDir"],
                         capture_output=True, text=True).stdout.strip()
    if old and os.path.abspath(old) != os.path.abspath(TEMPLATE):
        print("  ⚠ 已有 init.templateDir = %s，将被覆盖（原来的模板里的钩子不再生效）" % old)
    subprocess.run(["git", "config", "--global", "init.templateDir", TEMPLATE])
    print("  ✓ init.templateDir = %s" % TEMPLATE)

    # 3b) husky / lefthook 这类项目会设置 core.hooksPath，模板钩子根本不跑
    hp = subprocess.run(["git", "config", "--global", "--get", "core.hooksPath"],
                        capture_output=True, text=True).stdout.strip()
    if hp:
        print("  ⚠ 检测到 core.hooksPath = %s（husky/lefthook 常用）：" % hp)
        print("    设置了它之后 init.templateDir 里的钩子不会被调用，"
              "这些项目需要手动把 git-guard 接进它们的 hook 脚本。")

    # 3c) Windows 上 python 可能是 Microsoft Store 存根，先确认它真能跑
    py = shutil.which("python") or shutil.which("python3") or shutil.which("py")
    if not py:
        print("  ⚠ 没找到 python：钩子会在每次 commit/push 时提示跳过检查。")
    elif subprocess.run([py, "-c", ""], capture_output=True).returncode != 0:
        print("  ⚠ %s 不能执行 Python（可能是 Microsoft Store 占位存根），钩子会跳过检查。" % py)

    # 4) 词表模板（已存在则不动）
    terms = os.path.join(GUARD_HOME, "terms.txt")
    if not os.path.exists(terms):
        with open(terms, "w", encoding="utf-8") as f:
            f.write("# 自定义敏感词，每行一个（只存本地，勿入库）\n"
                    "# 例：你的品牌名 / 竞品名 / MSKU 前缀\n")
        print("  ✓ 新建词表 %s（请填入你的业务词）" % terms)
    else:
        print("  · 词表已存在，保持不动: %s" % terms)

    allow = os.path.join(GUARD_HOME, "allow.txt")
    if not os.path.exists(allow):
        with open(allow, "w", encoding="utf-8") as f:
            f.write("# 允许的正则，每行一个\n")
        print("  ✓ 新建放行表 %s" % allow)

    print("")
    print("完成。注意：")
    print("  · 只对之后新建/新克隆的仓库自动生效")
    print("  · 已有仓库补装：")
    for h in HOOK_NAMES:
        print("      cp %s/%-11s .git/hooks/" % (HOOKS, h))
    print("  · 老仓库先把存量记成基线，否则第一次推送会被历史遗留全挡住：")
    print("      python %s --all --write-baseline" % os.path.join(GUARD_HOME, "scan.py"))
    print("  · 服务端兜底（客户端钩子能被关掉，这层不能）：见 server/pre-receive")
    print("  · 自检： python %s --all" % os.path.join(GUARD_HOME, "scan.py"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
