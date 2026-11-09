#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安装 git-guard 钩子到全局 git 模板。"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD_HOME = os.path.join(os.path.expanduser("~"), ".git-guard")
TEMPLATE = os.path.join(os.path.expanduser("~"), ".git-templates")
HOOKS = os.path.join(TEMPLATE, "hooks")


def main():
    print("安装 git-guard")
    print("  脚本目录: " + HERE)

    os.makedirs(GUARD_HOME, exist_ok=True)
    os.makedirs(HOOKS, exist_ok=True)

    # 1) 扫描器
    shutil.copy2(os.path.join(HERE, "scan.py"), os.path.join(GUARD_HOME, "scan.py"))
    shutil.copy2(os.path.join(HERE, "sanitize.py"), os.path.join(GUARD_HOME, "sanitize.py"))
    print("  ✓ 扫描器 -> %s" % GUARD_HOME)

    # 2) 钩子
    for h in ("pre-commit", "pre-push"):
        dst = os.path.join(HOOKS, h)
        shutil.copy2(os.path.join(HERE, h), dst)
        try:
            os.chmod(dst, 0o755)
        except Exception:
            pass
        print("  ✓ 钩子 -> %s" % dst)

    # 3) 全局模板
    subprocess.run(["git", "config", "--global", "init.templateDir", TEMPLATE])
    print("  ✓ init.templateDir = %s" % TEMPLATE)

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
    print("      cp %s/pre-commit .git/hooks/" % HOOKS)
    print("      cp %s/pre-push   .git/hooks/" % HOOKS)
    print("  · 自检： python %s --all" % os.path.join(GUARD_HOME, "scan.py"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
