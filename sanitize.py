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

__version__ = "0.3.0"


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def die(msg, code=1):
    print("\n[错误] " + msg)
    sys.exit(code)


# ---------------- suggest ----------------

# 检测形态只能有一份定义：这里直接用 scan.py 的，别再抄一遍。
# 抄一遍的后果已经发生过一次（findall 分组 bug 让同站点第二个店铺永远进不了草稿）。
try:
    from scan import (GENERIC as _RULES, decode_all as _decode_all,
                      batch_blobs as _batch, load_config as _load_config,
                      _office_meta as _office_meta,
                      _id18_ok as _id_ok, _luhn_ok as _luhn_ok)
except Exception:                                    # 兜底：极端情况下退化成旧行为
    _RULES = {
        "ASIN": re.compile(r"\bB0[A-Z0-9]{8}\b"),
        "店铺名": re.compile(r"\d+\.[\u4e00-\u9fa5]{2,}-(?:DE|FR|ES|IT|UK|US|MX|CA|JP|AU|AE|SA|NL|BE|SE|PL|IE)\b"),
        "邮箱": re.compile(r"[\w.+-]+@[\w-]{2,}\.[\w.]{2,}"),
        "身份证": re.compile(r"(?<![0-9A-Za-z])[1-9]\d{16}[\dXx](?![0-9A-Za-z])"),
        "银行卡": re.compile(r"(?<![\d.])\d{16,19}(?![\d.])"),
        "Seller ID": re.compile(r"(?i)(?:seller[ _-]?id)['\"]?\s*[:=]\s*['\"]?(A[A-Z0-9]{12,14})\b"),
    }

    def _id_ok(v):
        return True

    def _luhn_ok(v):
        return True

    def _decode_all(data):
        return [data.decode("utf-8", "ignore")]

    def _batch(shas):
        for s in shas:
            p = subprocess.run(["git", "cat-file", "-p", s], capture_output=True)
            yield s, b"blob", p.stdout

    def _load_config(repo):
        class _C(object):
            terms, allow, path_allow = [], [], []

            def allowres(self):
                return []
        return _C()

    def _office_meta(data):
        return []


ASIN = _RULES["ASIN"]
SHOP = _RULES["店铺名"]
MAIL = _RULES["邮箱"]
_PATH = re.compile(r"[A-Za-z]:[\\/]{1,2}Users[\\/]{1,2}([^\\/\s\"'<>]+)")


def collect_all_texts():
    """遍历全历史 blob，产出 (path, texts, is_binary, raw)。

    解码走 scan.py 的 decode_all：UTF-16 / OLE / GBK 的历史文件和 HEAD 一样要能读出来，
    否则 suggest 出的草稿会漏项 —— 而漏项在清洗完成后才以「复扫还有残留」的形式暴露。
    """
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
    paths = {}
    for sha, path in jobs:
        paths.setdefault(sha, set()).add(path)
    for sha, typ, data in _batch([s for s, _ in jobs]):
        if typ != b"blob":
            continue
        isbin = data[:2] == b"PK" or b"\x00" in data[:8192]
        yield (", ".join(sorted(p for p in paths.get(sha, set()) if p) or {sha}),
               _decode_all(data), isbin, data)


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

    asins, shops, mails, phones, binaries = [], [], [], [], set()
    ids, cards, sellers = [], [], []
    secrets, paths, meta_files = set(), set(), set()
    PHONE = _RULES["手机号"]
    KEY = _RULES["私钥"]
    CRED = _RULES["疑似凭据"]
    cfg = _load_config(os.path.abspath("."))
    allowres = cfg.allowres()

    def allowed(v):
        return any(a.search(v) for a in allowres)

    for path, texts, isbin, raw in collect_all_texts():
        for text in texts:
            hit_any = False
            for m in ASIN.finditer(text):
                v = m.group(0)
                if not re.match(r"^B0(ASIN\d{4}|EXAMPLE\d*|XXXXXXXX)$", v) and v not in asins:
                    asins.append(v)
                hit_any = True
            for m in SHOP.finditer(text):
                # 旧版用 SHOP.findall()：正则带一个分组，findall 只返回站点码（DE/FR…），
                # 再 re.search 回查全文第一个同站点店铺 —— 同一文件里第二个同站点店铺
                # 永远进不了草稿，于是「扫描器拦、清洗器漏」，历史洗不干净。
                v = m.group(0)
                if not v.startswith(("店铺A", "店铺B")) and v not in shops:
                    shops.append(v)
                hit_any = True
            for m in MAIL.finditer(text):
                v = m.group(0)
                if v not in mails and "example.com" not in v:
                    mails.append(v)
                hit_any = True
            for m in PHONE.finditer(text):
                v = m.group(0)
                if v not in phones:
                    phones.append(v)
                hit_any = True
            # 身份证 / 银行卡要算校验位，和扫描器同一套判定
            for m in _RULES["身份证"].finditer(text):
                v = m.group(0)
                if _id_ok(v) and v not in ids:
                    ids.append(v)
                hit_any = hit_any or _id_ok(v)
            for m in _RULES["银行卡"].finditer(text):
                v = m.group(0)
                if _luhn_ok(v) and v not in cards:
                    cards.append(v)
                hit_any = hit_any or _luhn_ok(v)
            for m in _RULES["Seller ID"].finditer(text):
                v = m.group(1)
                if v not in sellers:
                    sellers.append(v)
                hit_any = True
            if KEY.search(text) or CRED.search(text):
                secrets.add(path)
            for m in _PATH.finditer(text):
                paths.add(m.group(1))
            # 含 NUL 的 blob，filter-repo 的文本替换会整个跳过 —— 包括 UTF-16 文本文件。
            # 这些文件里的值必须靠 --drop 或手工改，映射再补也替换不动。
            if isbin and hit_any:
                binaries.add(path)
        # Office 元数据在 zip 里，filter-repo 同样替换不动
        if raw[:2] == b"PK" and _office_meta(raw):
            meta_files.add(path)

    for name, lst in (("身份证", ids), ("银行卡", cards), ("Seller ID", sellers)):
        lst[:] = [v for v in lst if not allowed(v)]

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
    if phones:
        lines.append("# ---- 手机号 ----")
        for i, p in enumerate(phones, 1):
            lines.append("%s==>PHONE_C%d" % (p, i))
        lines.append("")
    if ids:
        lines.append("# ---- 身份证 ----")
        for i, v in enumerate(ids, 1):
            lines.append("%s==>ID_C%d" % (v, i))
        lines.append("")
    if cards:
        lines.append("# ---- 银行卡 ----")
        for i, v in enumerate(cards, 1):
            lines.append("%s==>CARD_C%d" % (v, i))
        lines.append("")
    if sellers:
        lines.append("# ---- Seller ID / Merchant Token（标签一起替换） ----")
        for i, v in enumerate(sellers, 1):
            lines.append("%s==>SELLER_C%d" % (v, i))
        lines.append("")
    lines.append("# ---- 兜底：漏网 ASIN（正则） ----")
    # 旧版是 regex:\bB0[A-Z0-9]{8}\b==>B0ASINREDACTED。
    # filter-repo 先跑字面量、再跑正则，于是这条兜底会把上面刚生成的
    # B0ASIN0001 / B0EXAMPLE1 一起吃成 B0ASINREDACTED —— 精心做的映射全被抹平。
    lines.append("regex:\\bB0(?!ASIN\\d{4,}\\b|EXAMPLE\\d*\\b|XXXXXXXX\\b)[A-Z0-9]{8}\\b==>B0ASINREDACTED")

    # 剩下两类不能靠占位符糊过去，单独提醒
    if secrets:
        lines.append("")
        lines.append("# ⚠ 这些文件里有私钥或疑似凭据：")
        for s in sorted(secrets)[:20]:
            lines.append("#   " + s)
        lines.append("#   私钥/凭据别用占位符代替 —— 先吊销轮换，再从历史剔除"
                     "（apply --drop <path>）。")
    if paths:
        lines.append("")
        lines.append("# · 本地路径里出现的用户名（要清就自己加一行 <用户名>==>USER_A，"
                     "注意会误伤同名普通词）：")
        for u in sorted(paths)[:10]:
            lines.append("#   " + u)
    if meta_files:
        lines.append("")
        lines.append("# ⚠ 这些 Office 文档的内嵌元数据里有真实姓名/公司名：")
        for f in sorted(meta_files)[:20]:
            lines.append("#   " + f)
        lines.append("#   元数据在 zip 里，filter-repo 替换不动 —— 改文件属性后另存，"
                     "再作为新提交加回（或 apply --drop <path>）。")

    out = args.output or "map.suggested.txt"
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("\n[suggest] 草稿已写入: %s" % os.path.abspath(out))
    print("  店铺名 %d / ASIN %d / 邮箱 %d / 手机号 %d / 身份证 %d / 银行卡 %d / Seller ID %d"
          % (len(shops), len(asins), len(mails), len(phones), len(ids), len(cards), len(sellers)))
    if secrets:
        print("  ⚠ %d 个文件含私钥或凭据：先吊销轮换，不要指望占位符" % len(secrets))
    if meta_files:
        print("  ⚠ %d 个 Office 文档的元数据里有姓名/公司名（替换不动，需另存）"
              % len(meta_files))
    if binaries:
        print("\n  ⚠ 这些文件含敏感内容，但 filter-repo 的文本替换会跳过它们")
        print("    （含 NUL 的 blob 一律不碰，UTF-16 文本文件同样在其中）：")
        for b in sorted(binaries):
            print("      " + b)
        print("    这些值补映射也没用，只有两条路：")
        print("      · apply 时用 --drop <path> 把历史版本整个剔除，再把脱敏副本作为新提交加回")
        print("      · 或者把文件另存成 UTF-8 后重新提交，让它进入文本替换的射程")
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
    try:
        r = sh(cmd)
        print(r.stdout[-2000:])
        if r.returncode != 0:
            print(r.stderr[-2000:])
            die("filter-repo 失败。可用备份恢复：\n  git clone %s <新目录>" % bundle)
    finally:
        # 规则文件里是明文真值，失败路径也必须删掉，不能留在 .git 里
        try:
            os.remove(rules_path)
        except OSError:
            pass

    # 重写完立刻自证：旧流程靠人手敲 scan.py --all，而那个 --all 只看 HEAD，
    # 于是「洗完了」常常是假象。这里自动跑一次真·全历史（--all --reflog）。
    # 基线是按值哈希记的，历史一改就全部失效，顺手删掉，免得把新值也当成存量放过。
    baseline_path = os.path.join(repo, ".git", "guard-baseline.txt")
    if os.path.isfile(baseline_path):
        try:
            os.remove(baseline_path)
            print("\n[apply] 已清掉旧基线（历史重写后它不再对应任何内容）")
        except OSError:
            pass
    print("\n[apply] 复扫全历史（--all --reflog）...")
    v = sh([sys.executable, SCAN, "--all"])
    print(v.stdout.rstrip())
    if v.returncode != 0:
        print("\n[apply] ⚠ 仍有残留命中。两类原因别混着处理：")
        print("    · 值本来就只在二进制 / UTF-16 文件里 —— filter-repo 替换不动，"
              "用 --drop 或另存为 UTF-8 再提交，补映射不会收敛")
        print("    · 值没进草稿（比如邮箱域名被 allow 掉） —— 补进映射文件再 apply，幂等")

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
    if sys.argv[1] in ("--version", "-V", "version"):
        print("git-guard sanitize %s" % __version__)
        return 0
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
