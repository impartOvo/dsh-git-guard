# 修过的漏检（bug 日志）

这里只记**已经修掉、并且有回归用例**的问题。每条都写了症状、根因、复现方式和对应用例；
拿不准的地方宁可写"只知道这么多"，也不编一个解释。

复现示例里的真实值一律打码（`B0****1234`、`138****5678`、`2.某店铺-DE`），
一是这个仓库自己也要过自己的扫描，二是别把要保护的东西抄进文档。

验证：`python tests/run_tests.py` → 16/16。

---

## 1. `--all` 只扫 HEAD，「清洗后复扫」给的是假清白

**症状**　`scan.py --all` 对只在其它分支 / 已删分支 / reflog 里的泄露报"未发现敏感业务数据"。

**影响**　这是 `sanitize.py apply` 之后的验收步骤。洗完照样说干净，等于清洗没有验收。

**根因**　`main()` 里写的是 `range_texts(["HEAD"])`，即 `git rev-list --objects HEAD`，
只覆盖 HEAD 可达对象。`--all` 这个名字是骗人的。

**复现**

```bash
git checkout -b side && echo 'B0****1234' > side.txt
git add side.txt && git commit -m 'side leak' --no-verify
git checkout main && echo '# clean' > c.txt && git add c.txt && git commit -m clean
git rev-list --count HEAD          # 1
git rev-list --count --all         # 2
python scan.py --all               # 修复前：✓ 未发现
```

**修复**　改为 `range_texts(["--all", "--reflog"])`；`apply` 结束自动跑一次并打印残留。

**用例**　`all_flag_covers_other_branches`

---

## 2. UTF-16 / 旧式 `.xls` / GBK 三种编码整段失明

**症状**　这些文件里放着真实的 ASIN、手机号、店铺名，扫描器一句"干净"就过了。

**影响**　不是边角情况：这是 Windows 工具，而 PowerShell 5.1 的 `>` / `Out-File` / `Export-Csv`
默认写 UTF-16LE；中文 Excel「另存为 CSV」默认写 GBK/GB18030。

**根因**　`decode()` 只有一条路：`data.decode("utf-8", "ignore")` 加一个 ZIP 解包。
其余编码全变成空串或被 NUL 打断，正则一条也匹配不上。

**复现**

```bash
# PowerShell 5.1
'B0****1234 138****5678' > v16.txt
git add v16.txt && python scan.py --staged      # 修复前：✓ 未发现
```

**修复**　`decode_all()` 按证据判定编码，不再无脑按 UTF-8：

| 证据 | 处理 |
| --- | --- |
| `PK` 头 | OOXML 解包，包内 xml 按 UTF-8 |
| BOM | 按 BOM 指定的编码 |
| OLE 头 `d0cf11e0` | UTF-16LE 解两种对齐（`.xls` / `.doc`） |
| 前 8KB 零字节 ≥ 1/8 | 按零字节奇偶位判 UTF-16LE/BE |
| 严格 UTF-8 可解 | 只按 UTF-8 扫 |
| 以上都不是 | UTF-8 / GB18030 / UTF-16LE 三份都扫 |

最后这一档是故意的：判定不出编码的二进制本来就不可信，宁可多一次正则。
反过来**不能**对所有文件都做多编码并集——把一份 UTF-8 中文按 UTF-16LE 再解一次，
乱码里很容易凑出 `<xx>@<yy>.<zz>` 的形状，于是干净文档开始误报（第一版就踩了这个坑，
被 `staged_clean_passes` 抓住）。

**用例**　`utf16le_is_scanned`、`utf16le_pure_cjk_term_is_scanned`、`legacy_xls_ole_is_scanned`、
`gbk_chinese_only_is_scanned`、`staged_clean_passes`

---

## 3. 提交信息从来没人看

**症状**　`git commit -m "出货 2.某店铺-DE 138****5678"` 一路畅通。

**影响**　消息进历史，和文件内容一样能被任何 clone 者看到。而 `sanitize.py` 反而用
`--replace-message` 去洗消息——入口不拦、出口才洗，顺序反了。

**根因**　只有 `pre-commit`（看暂存区）和 `pre-push`（看 blob），没有任何一处读提交信息。

**修复**　新增 `commit-msg` 钩子 + `scan.py --message FILE`；`--ranges-stdin`（推送扫描）
顺带扫范围内的 `%B`。

**用例**　`commit_message_is_scanned`、`range_scan_includes_messages`

---

## 4. `git mv` 被判成 rename 时，内容完全没扫

**症状**　改名提交能把一处老泄露"洗白"地重新提交一遍，扫描器全程沉默。

**影响**　静默通过，连提示都没有。

**根因**　`staged_texts()` 用 `git diff --cached --name-status -z` 按两元组硬切。
`-z` 下 rename 是三元组：

```
R100\0manual.txt\0handbook.txt\0
 → ('R100','manual.txt')   git show :manual.txt   已不在索引 → 空
 → ('handbook.txt','')     git show :               报错      → 空
```

两边都拿到空串，于是"扫过了、很干净"。

**复现**

```bash
# manual.txt 里已有历史遗留值（模拟早先 --no-verify 提交进去的）
git mv manual.txt handbook.txt
git diff --cached --name-status     # R100  manual.txt  handbook.txt
python scan.py --staged             # 修复前：✓ 未发现
```

边界：`git mv` 之后再改内容并 `git add`，相似度低于阈值时 git 报的是 `D`+`A`，
旧实现能拦住；只有**相似度仍 ≥50%（纯改名/小改）**才触发。

**修复**　改用 `git diff --cached --raw -z --no-renames --abbrev=40`：直接拿到新 blob 的
完整 sha，一个分支一套路径，不存在三元组；rename 退化成 A+D，新路径必然被扫。
（`--abbrev=40` 是必须的：`--raw` 默认给 7 位短 sha，和 `cat-file --batch` 返回的完整 sha 对不上，
第一版就因为这个把路径显示成了 sha。）

**用例**　`rename_with_same_content_is_scanned`、`rename_with_edit_is_scanned`

---

## 5. `suggest` 同站点只采第一个店铺 → 扫描器拦、清洗器漏

**症状**　一个文件里有三个店铺，`map.suggested.txt` 只出两个。

**影响**　清洗后历史里还留着真实店铺名，下次 `scan.py --all` 照样拦。
用户会以为工具坏了，然后开始 `--no-verify`。

**根因**　两处：

1. `SHOP.findall()` 的正则带一个分组，`findall` 只返回站点码（`DE`/`FR`），
   再 `re.search` 回查全文**第一个**同站点店铺——同站点的第二个永远进不了草稿。
2. 占位符编号是 `chr(64 + (i % 26) + 1)`：第一个是 `店铺B`（不是 A），第 26 个又回到 A，
   第 27 个和第 1 个撞名。

**复现**

```bash
# a.txt 里有 2.某店铺-DE / 5.某店铺-DE / 9.某店铺-FR
python sanitize.py suggest
# 修复前：店铺名 2 个；2.…-DE==>店铺B-DE；5.…-DE 不见了
```

**修复**　`SHOP.finditer()` + `m.group(0)`；编号改 `_label()`（1→A，27→AA），保证单射。

**用例**　`suggest_collects_every_store`

---

## 6. 兜底正则把刚生成的占位符吃掉

**症状**　`apply` 之后，几个不同 ASIN 全变成同一个 `B0ASINREDACTED`。

**影响**　映射白做，可读性归零，而且不报错——没人会发现。

**根因**　`git-filter-repo --replace-text` 的执行顺序是**先跑全部字面量、再跑正则**
（`apply_replace_text()` 与 `blob_callback` 两处实现一致）。草稿末尾那条
`regex:\bB0[A-Z0-9]{8}\b` 因此作用在字面量映射**之后**，把 `B0ASIN0001` 一起吞了。

**复现**

```python
rule = r"\bB0[A-Z0-9]{8}\b"
re.sub(rule, "B0ASINREDACTED", "B0ASIN0001")     # B0ASINREDACTED —— 占位符被吃
```

**修复**　兜底加负向断言：`\bB0(?!ASIN\d{4,}\b|EXAMPLE\d*\b|XXXXXXXX\b)[A-Z0-9]{8}\b`；
`scan.py` 的 `PLACEHOLDER` 同步放宽到 `ASIN\d{4,}`。

**用例**　`fallback_regex_keeps_placeholders`

---

## 7. 每个 blob 起一个子进程：802 个对象 118 秒

**症状**　首次推一个几百文件的仓库，钩子黑屏几分钟；分支多了更久。

**影响**　和"金额误报"是同一个机制——用户为了不等，学会 `--no-verify`。

**根因**　`staged_texts()` / `range_texts()` 对每个对象 `git cat-file -p`。

**实测**（同一台机器，802 个对象）

| 方式 | 耗时 |
| --- | --- |
| 逐 blob `git cat-file -p` | 118.50 s |
| 单次 `git cat-file --batch` | 0.44 s |
| 修复后 `scan.py --all`（含 python 启动） | 1.15 s |

**修复**　`batch_blobs()` 一次 `--batch` 流式读，按头部 `sha type size` 切分。

**用例**　`full_history_is_fast`（200 个对象 < 20s）

---

## 8. 命中值明文打印

**症状**　`report()` 把手机号、邮箱、凭据原样打到屏幕上。

**影响**　本地终端无所谓；钩子在 CI / 远程开发机 / 共享终端里跑时，这段输出会把要保护的
东西再抄进日志一份。

**修复**　默认对 `手机号`/`邮箱`/`疑似凭据`/`私钥`/`自定义词` 打码（`13***78 (len=11)`），
`--show-values` 才给明文；ASIN 和店铺名不打码（要看清才好改）。

**用例**　`pii_is_masked_by_default`

---

## 9. 钩子的四种静默失效

| 现象 | 根因 | 修复 |
| --- | --- | --- |
| 提交直接放行，无提示 | `command -v python` 找到的是 Microsoft Store 占位存根，执行返回非 0 | 钩子先 `"$PY" -c ""` 探测，不通过就明确警告并放行 |
| 每次提交都被判"扫描器异常" | `report()` 打印 `✓`(U+2713)，输出重定向到 cp936 文件时 `UnicodeEncodeError` → 外层 `except` 变 exit 2 | 钩子固定 `PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1` |
| 装了钩子却从不触发 | 项目设了 `core.hooksPath`（husky/lefthook），`init.templateDir` 被完全绕过 | `install.py` 检测并明确提示 |
| 别人的模板钩子一起失效 | `install.py` 静默覆盖已有 `init.templateDir` | 覆盖前打印原值 |

---

## 10. `sanitize.py` 的安全网自己不设防

| 问题 | 修复 |
| --- | --- |
| `git bundle create` 之后没有 `verify`，备份可能根本读不回来 | 建完立刻 `git bundle verify`，失败即中止 |
| 浅克隆只打印一句警告照跑，`filter-repo` 按片段重写 → 历史残缺 | 直接拒，`--allow-shallow` 才放行 |
| 工作区有改动也照跑，重写把未提交内容冲掉 | 已跟踪文件有改动即停；未跟踪文件只提示（`map.txt` 就躺在仓库里，不能因此卡死自己） |
| 清洗完靠人手敲复扫，而那个复扫当时只看 HEAD | `apply` 末尾自动 `--all --reflog` 复扫并列出残留 |
| `bundle --all` 不含 stash / reflog，重写后它们变悬空提交 | 检测到 stash 时明确提示 |

---

## 端到端验收

在一次性仓库上跑完整破坏性流程（含 `--drop` 剔除二进制、提交信息重写、自动复扫）：

```
构造：a.txt（店铺×2、ASIN×2、邮箱、手机号）、report.xlsx（二进制含 ASIN）、
      b.txt（手机号）、一条含店铺名和手机号的提交信息
suggest → 店铺名 3 个（含同站点第二个）、ASIN 2 个、邮箱 1 个，并点名 report.xlsx
apply --drop report.xlsx
  → 备份创建 + verify 通过
  → filter-repo 重写
  → 自动复扫：全部干净
结果：a.txt = 店铺A-DE 店铺B-DE B0ASIN0001 B0ASIN0002 contact1@example.com PHONE_C1
      提交信息 = 出货 店铺A-DE PHONE_C1
      xlsx 已不在历史里；全历史 blob 逐个 grep 残留 = 0
```

顺带验证两个守卫：浅克隆 → 拒绝；已跟踪文件有改动 → 拒绝。
