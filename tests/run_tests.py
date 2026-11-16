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


@test
def nul_heavy_binary_ascii_run_is_scanned(tmp):
    """零字节占主导的文件（稀疏/补齐的二进制）里夹着的 ASCII 文本不能丢"""
    repo = new_repo(tmp)
    write_bytes(os.path.join(repo, "pad.bin"),
                b"\x00" * 65536 + ("needle " + ASIN + " tail").encode())
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 1, "零字节多的文件被当成纯 UTF-16 解，ASCII 段落全部失明：\n" + out
    assert "ASIN" in out, out
    return out


@test
def ole_compressed_8bit_string_is_scanned(tmp):
    """BIFF8 的 8 位压缩字符串也要能看到（不能只解 UTF-16）"""
    repo = new_repo(tmp)
    body = b"\x00" * 40 + b"SST " + ASIN.encode("ascii")
    write_bytes(os.path.join(repo, "old.xls"),
                b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + body)
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 1, "OLE 里的 8 位串没扫到：\n" + out
    assert "ASIN" in out, out
    return out


@test
def identical_blobs_report_every_path(tmp):
    """同一份内容挂在多个路径上时，不能只报最后一个"""
    repo = new_repo(tmp)
    write(os.path.join(repo, "a.txt"), ASIN + "\n")
    shutil.copyfile(os.path.join(repo, "a.txt"), os.path.join(repo, "b.txt"))
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 1, out
    assert "a.txt" in out and "b.txt" in out, "只报了一个路径：\n" + out
    return out


@test
def custom_term_is_not_leaked_in_label(tmp):
    """自定义词打码时，分类标签不能把明文又印出来"""
    repo = new_repo(tmp)
    word = _s("品牌", "甲")
    terms = os.path.join(repo, ".terms")
    write(terms, word + "\n")
    write(os.path.join(repo, "a.txt"), "说明里提到 " + word + " 这个词\n")
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged", env_extra={"GIT_GUARD_TERMS": terms})
    assert rc == 1, out
    assert word not in out, "标签把自定义词明文带出来了：\n" + out
    rc, out2 = scan(repo, "--staged", "--show-values",
                    env_extra={"GIT_GUARD_TERMS": terms})
    assert word in out2, "--show-values 应给出明文：\n" + out2
    return out


@test
def suggest_reads_utf16_history(tmp):
    """suggest 的解码必须和 scan 一致，否则草稿漏项、洗完复扫还是红"""
    repo = new_repo(tmp)
    write_bytes(os.path.join(repo, "u16.txt"),
                b"\xff\xfe" + ("订单 %s\n" % ASIN).encode("utf-16-le"))
    add_commit(repo, None, "data")
    out_map = os.path.join(repo, "map.txt")
    r = sh([PY, SANITIZE, "suggest", "--output", out_map], repo)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    text = io.open(out_map, encoding="utf-8").read()
    assert ASIN in text, "UTF-16 历史文件里的 ASIN 没进草稿：\n" + text
    return text


@test
def suggest_emits_phone_lines(tmp):
    """suggest 要覆盖手机号（否则清完复扫仍被拦，得靠人手补）"""
    repo = new_repo(tmp)
    write(os.path.join(repo, "a.txt"), ("联系电话 %s\n" % PHONE))
    add_commit(repo, None, "data")
    out_map = os.path.join(repo, "map.txt")
    r = sh([PY, SANITIZE, "suggest", "--output", out_map], repo)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    text = io.open(out_map, encoding="utf-8").read()
    assert PHONE in text and "PHONE_C" in text, "草稿里没有手机号映射：\n" + text
    return text


@test
def suggest_flags_nul_files_it_cannot_rewrite(tmp):
    """含 NUL 的 blob（含 UTF-16 文本）filter-repo 替换不动，suggest 必须提前点名"""
    repo = new_repo(tmp)
    write_bytes(os.path.join(repo, "u16.txt"),
                b"\xff\xfe" + ("订单 %s\n" % STORE_FR).encode("utf-16-le"))
    add_commit(repo, None, "data")
    out_map = os.path.join(repo, "map.txt")
    r = sh([PY, SANITIZE, "suggest", "--output", out_map], repo)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    out = r.stdout.decode("utf-8", "replace")
    assert "u16.txt" in out, "没点名那个替换不动的文件：\n" + out
    assert "补映射也没用" in out, out
    return out


@test
def oversized_blob_tail_is_scanned(tmp):
    """超大对象只扫头段时，末尾的内容会被整段漏掉；尾部必须一起看，并如实报覆盖不足"""
    repo = new_repo(tmp)
    write_bytes(os.path.join(repo, "pad.bin"),
                b"\x00" * (4 * 1024 * 1024) + ("tail " + ASIN).encode())
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged", env_extra={"GIT_GUARD_MAX_BLOB": str(1024 * 1024)})
    assert rc == 1, "超大对象的尾部没有被扫描：\n" + out
    assert "ASIN" in out, out
    assert "可打印串抽取" in out, "覆盖不足必须如实报出来：\n" + out
    return out


@test
def baseline_downgrades_existing_hits(tmp):
    """存量记成基线后只提示不阻断，新增值照拦"""
    repo = new_repo(tmp)
    write(os.path.join(repo, "old.txt"), ASIN + "\n")
    add_commit(repo, None, "legacy")
    r = sh([PY, SCAN, "--all", "--write-baseline"], repo)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert os.path.isfile(os.path.join(repo, ".git", "guard-baseline.txt"))
    # 同一个值再出现：降级为存量
    write(os.path.join(repo, "old2.txt"), ASIN + "\n")
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 0, "存量值不该再阻断：\n" + out
    assert "基线" in out, out
    # 新值照拦
    write(os.path.join(repo, "new.txt"), ASIN2 + "\n")
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 1, "新增值必须照拦：\n" + out
    assert ASIN2 in out, out
    return out


@test
def path_allow_from_repo_config(tmp):
    """path-allow 让某个目录整体不参与扫描"""
    repo = new_repo(tmp)
    write(os.path.join(repo, ".git-guard.conf"), "path-allow: vendor/**\n")
    write(os.path.join(repo, "vendor", "x.txt"), ASIN + "\n")
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 0, "path-allow 没生效：\n" + out
    assert "path-allow" in out, out
    return out


@test
def term_from_local_repo_config(tmp):
    """仓库本地配置里的 term 生效（.git/guard.conf 不会入库）"""
    repo = new_repo(tmp)
    word = _s("品牌", "乙")
    write(os.path.join(repo, ".git", "guard.conf"), "term: %s\n" % word)
    write(os.path.join(repo, "a.txt"), "说明 " + word + "\n")
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 1, "仓库级词表没生效：\n" + out
    assert "自定义词" in out, out
    assert word not in out, "打码失败：\n" + out
    return out


@test
def disable_check_from_config(tmp):
    """disable 关掉某一类检查，其余照旧"""
    repo = new_repo(tmp)
    write(os.path.join(repo, ".git-guard.conf"), "disable: 手机号\n")
    write(os.path.join(repo, "a.txt"), PHONE + "\n")
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 0, out
    assert "已按配置关掉" in out, out
    write(os.path.join(repo, "b.txt"), ASIN + "\n")
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 1, "只该关掉手机号那一类：\n" + out
    assert "ASIN" in out and "[手机号]" not in out, out
    return out


@test
def credential_shapes_are_caught(tmp):
    """常见凭据形态：GitHub token、AWS key id"""
    repo = new_repo(tmp)
    gh = "gh" + "p_" + "a" * 36
    aws = "AKIA" + "Q" * 16
    write(os.path.join(repo, "a.env"), "T=%s\nK=%s\n" % (gh, aws))
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 1, out
    assert "疑似凭据" in out, out
    return out


@test
def cn_id_checksum_decides(tmp):
    """身份证要算校验位：格式对但校验位不对的不报"""
    repo = new_repo(tmp)
    good = "110105" + "19491231" + "002X"
    bad = "110105" + "19491231" + "0021"
    write(os.path.join(repo, "bad.txt"), bad + "\n")
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 0, "校验位不对的不该报：\n" + out
    write(os.path.join(repo, "good.txt"), good + "\n")
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 1, "合法身份证要报：\n" + out
    assert "身份证" in out, out
    return out


@test
def bank_card_needs_luhn(tmp):
    """银行卡号走 Luhn，过不了的当业务数字放行"""
    repo = new_repo(tmp)
    no_card = _s("4411", "1111", "1111", "1112")
    yes_card = _s("4111", "1111", "1111", "1111")
    write(os.path.join(repo, "no.txt"), no_card + "\n")
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 0, "Luhn 不通的不该报：\n" + out
    write(os.path.join(repo, "yes.txt"), yes_card + "\n")
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 1, out
    assert "银行卡" in out, out
    return out


@test
def office_metadata_is_reported(tmp):
    """docx 的作者 / 最后修改人是真名要报，Office 默认值不报"""
    repo = new_repo(tmp)

    def make(path, creator):
        z = zipfile.ZipFile(path, "w")
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w/>")
        z.writestr("docProps/core.xml",
                   '<cp:coreProperties><dc:creator>%s</dc:creator>'
                   '<cp:lastModifiedBy>%s</cp:lastModifiedBy></cp:coreProperties>'
                   % (creator, creator))
        z.close()

    make(os.path.join(repo, "default.docx"), "Microsoft Office User")
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 0, "Office 默认作者不该报：\n" + out
    name = _s("张", "三")
    make(os.path.join(repo, "real.docx"), name)
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 1, "真实姓名的元数据要报：\n" + out
    assert "Office元数据" in out, out
    assert name not in out, "姓名要打码：\n" + out
    return out


@test
def ref_name_is_scanned(tmp):
    """分支名/tag 名也会泄露"""
    repo = new_repo(tmp)
    write(os.path.join(repo, "a.txt"), "# clean\n")
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--ref", "refs/heads/" + STORE_DE + "-fix")
    assert rc == 1, "分支名没被扫：\n" + out
    assert "店铺名" in out, out
    return out


@test
def internal_submodule_url_is_scanned(tmp):
    """.gitmodules 里的内网地址要报"""
    repo = new_repo(tmp)
    write(os.path.join(repo, ".gitmodules"),
          '[submodule "x"]\n\tpath = x\n\turl = http://10.0.0.5/x.git\n')
    sh(["git", "add", "-A"], repo)
    rc, out = scan(repo, "--staged")
    assert rc == 1, out
    assert "子模块地址" in out, out
    return out


@test
def large_blob_uses_runs_and_stays_fast(tmp):
    """8MB 对象走可打印串抽取：中间段能覆盖到，且不该慢"""
    repo = new_repo(tmp)
    write_bytes(os.path.join(repo, "mid.bin"),
                b"\x00" * (4 * 1024 * 1024) + ("mid " + ASIN + " tail").encode()
                + b"\x00" * (4 * 1024 * 1024))
    sh(["git", "add", "-A"], repo)
    t = time.time()
    rc, out = scan(repo, "--staged")
    d = time.time() - t
    assert rc == 1, "8MB 对象中间段没扫到：\n" + out
    assert d < 15, "大对象太慢：%.1fs" % d
    return "%.2fs" % d


@test
def commit_mode_scans_message_and_tree(tmp):
    """post-commit 用的单提交模式：内容 + 提交信息都要看"""
    repo = new_repo(tmp)
    write(os.path.join(repo, "a.txt"), "# clean\n")
    sh(["git", "add", "-A"], repo)
    sh(["git", "commit", "-q", "--no-verify", "-m", "出货 " + PHONE], repo)
    rc, out = scan(repo, "--commit", "HEAD")
    assert rc == 1, out
    assert "手机号" in out and "<commit message>" in out, out
    return out


@test
def server_stdin_scans_incoming(tmp):
    """pre-receive 协议：<old> <new> <ref>"""
    repo = new_repo(tmp)
    base = sh(["git", "rev-parse", "HEAD"], repo).stdout.decode().strip()
    write(os.path.join(repo, "a.txt"), ASIN + "\n")
    add_commit(repo, None, "incoming")
    head = sh(["git", "rev-parse", "HEAD"], repo).stdout.decode().strip()
    rc, out = scan(repo, "--server-stdin",
                   input_bytes=("%s %s refs/heads/main\n" % (base, head)).encode())
    assert rc == 1, "服务端扫描没看到新提交：\n" + out
    assert "ASIN" in out, out
    return out


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
