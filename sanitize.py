#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git-guard sanitize — 手动历史脱敏向导（低频、破坏性、需人确认）

两个子命令：

  suggest  扫描全历史，生成映射草稿 map.suggested.txt
           只读，不改任何东西。

  apply <mapfile>
           按映射重写历史。会先打包备份，再调 git filter-repo。
           完成后再手动 force push。

映射文件格式（每行一条，先长后短）：
  原值==>占位符
  某品牌名==>BRAND_A
  B0EXAMPLE1==>ASIN_C1
  regex:^B0[A-Z]{2}[A-Z0-9]{6}$==>B0ASINREDACTED
"""
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCAN = os.path.join(HERE, "scan.py")


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def die(msg, code=1):
    print("\n[错误] " + msg)
    sys.exit(code)


# ---------------- suggest ----------------

ASIN = re.compile(r"\bB0[A-Z0-9]{8}\b")
SHOP = re.compile(r"\d+\.[\u4e00-\u9fa5]{2,}-(DE|FR|ES|IT|UK|US|MX|CA|JP|AU|AE|SA|NL|BE|SE|PL|IE)\b")
MAIL = re.compile(r"[\w.+-]+@[\w-]{2,}\.[\w.]{2,}")


def collect_all_texts():
    """遍历全历史 blob，产出 (path, text)。二进制文档解包。"""
    import io
    import zipfile
    out = sh(["git", "rev-list", "--objects", "HEAD"]).stdout
    jobs, seen = [], set()
    for line in out.splitlines():
        p = line.split(" ", 1)
        sha = p[0]
        path = p[1] if len(p) > 1 else ""
        if sha in seen:
            continue
        seen.add(sha)
        jobs.append((sha, path))
    if not jobs:
        return
    bc = subprocess.run(["git", "cat-file", "--batch-check"],
                        input="\n".join(s for s, _ in jobs).encode(),
                        capture_output=True).stdout.decode("utf-8", "ignore")
    kinds = [l.split()[1] if len(l.split()) > 1 else "?" for l in bc.splitlines()]
    for (sha, path), k in zip(jobs, kinds):
        if k != "blob":
            continue
        data = subprocess.run(["git", "cat-file", "-p", sha], capture_output=True).stdout
        text = data.decode("utf-8", "ignore")
        if data[:2] == b"PK":
            try:
                z = zipfile.ZipFile(io.BytesIO(data))
                parts = [n for n in z.namelist() if n.endswith((".xml", ".rels", ".txt"))]
                if parts:
                    text = b"\n".join(z.read(n) for n in parts).decode("utf-8", "ignore")
            except Exception:
                pass
        yield (path or sha), text, data[:2] == b"PK"


def _label(i):
    """1→A … 26→Z，27→AA。保证占位符单射，不撞名。"""
    s, i = "", i - 1
    while True:
        s = chr(65 + i % 26) + s
        i = i // 26 - 1
        if i < 0:
            return s


def cmd_suggest(args):
    if sh(["git", "rev-parse", "--verify", "HEAD"]).returncode != 0:
        die("当前目录不是 git 仓库，或没有任何提交")
    print("[git-guard] 正在读取全历史，文件多时请稍候...")

    asins, shops, mails, binaries = [], [], [], set()
    for path, text, isbin in collect_all_texts():
        if isbin and (ASIN.search(text) or MAIL.search(text)):
            binaries.add(path)
        for m in ASIN.findall(text):
            if not re.match(r"^B0(ASIN\d{4}|EXAMPLE\d*|XXXXXXXX)$", m) and m not in asins:
                asins.append(m)
        for m in SHOP.finditer(text):
            # 旧版用 SHOP.findall()：正则带一个分组，findall 只返回站点码（DE/FR…），
            # 再 re.search 回查全文第一个同站点店铺 —— 同一文件里第二个同站点店铺
            # 永远进不了草稿，于是「扫描器拦、清洗器漏」，历史洗不干净。
            v = m.group(0)
            if not v.startswith(("店铺A", "店铺B")) and v not in shops:
                shops.append(v)
        for m in MAIL.findall(text):
            if m not in mails and "example.com" not in m:
                mails.append(m)

    lines = ["# git-guard 映射草稿（自动生成 %s）" % time.strftime("%Y-%m-%d %H:%M:%S"),
             "# 逐行检查：改占位符名、删掉不该动的、补上漏掉的。确认后再 apply。",
             ""]
    if shops:
        lines.append("# ---- 店铺名 ----")
        for i, s in enumerate(shops, 1):
            # 旧版 chr(64 + (i % 26) + 1)：第 1 个是 B（不是 A），第 26 个又回到 A，
            # 第 27 个与第 1 个撞名 —— 两个真实店铺映射到同一占位符，映射不再单射。
            lines.append("%s==>店铺%s-%s" % (s, _label(i), s.rsplit("-", 1)[-1]))
        lines.append("")
    if asins:
        lines.append("# ---- ASIN ----")
        for i, a in enumerate(asins, 1):
            lines.append("%s==>B0ASIN%04d" % (a, i))
        lines.append("")
    if mails:
        lines.append("# ---- 邮箱 ----")
        for i, m in enumerate(mails, 1):
            lines.append("%s==>contact%d@example.com" % (m, i))
        lines.append("")
    lines.append("# ---- 兜底：漏网 ASIN（正则） ----")
    # 旧版是 regex:\bB0[A-Z0-9]{8}\b==>B0ASINREDACTED。
    # filter-repo 先跑字面量、再跑正则，于是这条兜底会把上面刚生成的
    # B0ASIN0001 / B0EXAMPLE1 一起吃成 B0ASINREDACTED —— 精心做的映射全被抹平。
    lines.append("regex:\\bB0(?!ASIN\\d{4,}\\b|EXAMPLE\\d*\\b|XXXXXXXX\\b)[A-Z0-9]{8}\\b==>B0ASINREDACTED")

    out = args.output or "map.suggested.txt"
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("\n[suggest] 草稿已写入: %s" % os.path.abspath(out))
    print("  店铺名 %d 个 / ASIN %d 个 / 邮箱 %d 个" % (len(shops), len(asins), len(mails)))
    if binaries:
        print("\n  ⚠ 以下二进制文档含敏感内容：filter-repo 的文本替换会跳过它们。")
        print("    建议在 apply 时对它们用 --invert-paths 剔除历史版本，再把脱敏副本作为新提交加回：")
        for b in sorted(binaries):
            print("      " + b)
    print("\n  下一步：人工检查 %s，然后运行" % out)
    print("    python sanitize.py apply %s" % out)
    return 0


# ---------------- apply ----------------

def cmd_apply(args):
    mapfile = args.mapfile
    if not os.path.isfile(mapfile):
        die("找不到映射文件: " + mapfile)

    if sh(["git", "rev-parse", "--verify", "HEAD"]).returncode != 0:
        die("当前目录不是 git 仓库")
    if sh(["git", "filter-repo", "--version"]).returncode != 0:
        die("未安装 git-filter-repo。安装： pip install git-filter-repo")

    with open(mapfile, "r", encoding="utf-8") as f:
        rules = [l for l in f.read().splitlines() if l.strip() and not l.startswith("#")]
    if not rules:
        die("映射文件里没有有效规则")
    bad = [r for r in rules if "==>" not in r]
    if bad:
        die("这些行缺少 ==> ：" + "; ".join(bad[:5]))

    repo = os.path.abspath(".")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bundle = os.path.join(os.path.dirname(repo), "guard-backup-%s.bundle" % stamp)

    print("[apply] 目标仓库: %s" % repo)
    print("[apply] 规则 %d 条" % len(rules))
    print("[apply] 规则前 3 条：")
    for r in rules[:3]:
        print("        " + r)

    if not args.yes:
        print("")
        print("  这是不可逆操作：所有 commit hash 将被重写，需要 force push。")
        if os.path.exists(os.path.join(repo, ".git", "shallow")):
            print("  注意：这是浅克隆，filter-repo 可能需要完整历史。")
        ans = input("  输入 yes 继续: ").strip().lower()
        if ans != "yes":
            print("[apply] 已取消，未做任何改动。")
            return 0

    # 前置体检：任何一项不满足就停，别把仓库带进半重写的状态
    # 1) 浅克隆：filter-repo 会按看到的片段重写，历史直接残缺
    if os.path.exists(os.path.join(repo, ".git", "shallow")) and not args.allow_shallow:
        die("这是浅克隆（.git/shallow 存在），重写只会产出残缺历史。\n"
            "  先 git fetch --unshallow，或加 --allow-shallow 自担风险。")
    # 2) 工作区：已跟踪文件有改动 -> 停（重写会把它们冲掉）；
    #    只有未跟踪文件 -> 只提示（filter-repo 不碰未跟踪文件，
    #    而 suggest 生成的 map.txt 就躺在仓库里，不能因此把自己卡死）
    status = sh(["git", "status", "--porcelain"]).stdout.splitlines()
    tracked = [l for l in status if not l.startswith("??")]
    untracked = [l[3:] for l in status if l.startswith("??")]
    if tracked and not args.force:
        die("工作区有未提交改动，先提交/stash 再清洗：\n" + "\n".join(tracked[:20]))
    skip = os.path.abspath(mapfile)
    untracked = [p for p in untracked if os.path.abspath(p) != skip]
    if untracked:
        print("\n[apply] · 有 %d 个未跟踪文件（不会被重写影响，也不进备份）：%s"
              % (len(untracked), ", ".join(untracked[:5])))
    # 3) stash 里可能有真实数据，bundle --all 保不住它们
    stashes = sh(["git", "stash", "list"]).stdout.strip()
    if stashes:
        print("\n[apply] ⚠ stash 里有 %d 条记录，bundle --all 不包含 stash；"
              "重写后它们会变成悬空提交。" % len(stashes.splitlines()))

    # 备份
    print("\n[apply] 备份到 %s" % bundle)
    r = sh(["git", "bundle", "create", bundle, "--all"])
    if r.returncode != 0:
        die("备份失败，已中止：\n" + r.stderr)
    # 备份是这条路上唯一的安全网，必须验证它真能读回来
    v = sh(["git", "bundle", "verify", bundle])
    if v.returncode != 0:
        die("备份包校验失败，已中止：\n" + (v.stderr or v.stdout))
    print("[apply] 备份完成并校验通过")

    # 规则落盘（filter-repo 需要文件路径）
    rules_path = os.path.join(repo, ".git", "guard-rules.txt")
    with open(rules_path, "w", encoding="utf-8") as f:
        f.write("\n".join(rules) + "\n")

    cmd = ["git", "filter-repo", "--force",
           "--replace-text", rules_path,
           "--replace-message", rules_path]
    if args.drop:
        for p in args.drop:
            cmd += ["--path", p]
        cmd += ["--invert-paths"]
    print("\n[apply] 执行: " + " ".join(cmd[:4]) + " ...")
    r = sh(cmd)
    print(r.stdout[-2000:])
    if r.returncode != 0:
        print(r.stderr[-2000:])
        die("filter-repo 失败。可用备份恢复：\n  git clone %s <新目录>" % bundle)

    os.remove(rules_path)

    # 重写完立刻自证：旧流程靠人手敲 scan.py --all，而那个 --all 只看 HEAD，
    # 于是「洗完了」常常是假象。这里自动跑一次真·全历史（--all --reflog）。
    print("\n[apply] 复扫全历史（--all --reflog）...")
    v = sh([sys.executable, SCAN, "--all"])
    print(v.stdout.rstrip())
    if v.returncode != 0:
        print("\n[apply] ⚠ 仍有残留命中，别急着重推。"
              "把上面列出的值补进映射文件，再 apply 一次（幂等）。")

    print("\n[apply] 历史已重写。下一步（手动）：")
    print("  1) git remote add origin <你的仓库URL>    # filter-repo 会删掉 origin")
    print("  2) git push --force --mirror origin")
    print("  3) 复扫确认： python %s --all" % SCAN)
    print("  4) 其它机器需重新 clone（旧 hash 已失效）")
    print("\n[apply] 备份包保留在: %s" % bundle)
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    sub = sys.argv[1]
    if sub == "suggest":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--output", "-o")
        return cmd_suggest(ap.parse_args(sys.argv[2:]))
    if sub == "apply":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("mapfile")
        ap.add_argument("--yes", "-y", action="store_true")
        ap.add_argument("--drop", nargs="*", default=[],
                        help="额外从历史剔除的路径（二进制文档请放这里）")
        ap.add_argument("--allow-shallow", action="store_true",
                        help="浅克隆也继续（历史会残缺，慎用）")
        ap.add_argument("--force", action="store_true",
                        help="工作区有未提交改动也继续")
        return cmd_apply(ap.parse_args(sys.argv[2:]))
    print("未知子命令: " + sub)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
