# dsh-git-guard

提交/推送前的敏感业务数据拦截，加上低频使用的历史脱敏向导。

面向"仓库里混进了真实店铺名、ASIN、订单金额"这类问题：**Git 不会真的删掉东西**，`git rm` 只能让最新快照不含该文件，历史 blob 仍可被任何 clone 者取出。所以需要两层：

| 层 | 触发 | 作用 |
|---|---|---|
| 钩子 | 自动，每次 `commit` / `push` | 拦住新进来的敏感数据 |
| 脱敏向导 | 手动，低频 | 清洗已经进了历史的 |

---

## 一、自动层：钩子

### 安装

```bash
git clone https://github.com/<you>/dsh-git-guard.git
cd dsh-git-guard
python install.py
```

`install.py` 做三件事：

1. 把 `scan.py` / `sanitize.py` 复制到 `~/.git-guard/`
2. 把 `pre-commit` / `commit-msg` / `pre-push` 装到 `~/.git-templates/hooks/`
3. 设置 `git config --global init.templateDir ~/.git-templates`

装完会顺手体检你的环境：`core.hooksPath` 是否被 husky/lefthook 占着、
`python` 是不是 Microsoft Store 的占位存根、`init.templateDir` 是否会被覆盖 —— 这三种情况都会让钩子静默失效。

**只对之后新建/新克隆的仓库自动生效。** 已有仓库补装：

```bash
cp ~/.git-templates/hooks/pre-commit .git/hooks/
cp ~/.git-templates/hooks/commit-msg .git/hooks/
cp ~/.git-templates/hooks/pre-push   .git/hooks/
```

### 检查什么

通用模式（不含任何具体业务标识，可安全公开）：

| 类型 | 说明 |
|---|---|
| ASIN | `B0` + 8 位字母数字 |
| 店铺名 | `编号.中文名-站点`，如 `2.某店铺-DE` |
| **手机号** | 中国手机号；已排除 `(16392411031)` 这类括号包裹的 browse node |
| **邮箱** | 标准邮箱形态 |
| 私钥 | `-----BEGIN ... PRIVATE KEY-----` |
| 疑似凭据 | `api_key` / `secret` / `password` 后跟 12 位以上值 |
| 本地路径 | `C:\Users\<用户名>` |
| **提交信息** | `commit-msg` 钩子扫 `-m` 里的同一套规则 |

**编码不是只认 UTF-8。** 同一个文件会按证据判定编码后解码，下面三种过去整段失明：

| 场景 | 现成来源 |
| --- | --- |
| UTF-16LE | PowerShell 5.1 的 `>` / `Out-File` / `Export-Csv` 默认编码 |
| UTF-16（OLE 复合文档） | 旧式 `.xls` / `.doc` |
| GBK / GB18030 | 中文 Excel「另存为 CSV」 |

docx/xlsx 这类 OOXML 走 zip 解包；判定不出编码的二进制（既不是合法 UTF-8、也没有 UTF-16 的零字节特征）会三种解码都扫一遍，宁可多一次正则。

**业务数字不在检测范围内**：金额、销量、订单数、曝光、编号一律不报。
理由：报表里数字无处不在，拦了必然逼出习惯性 `--no-verify`，防护反而失效。
真正能定位到人的是**标识 + 联系方式**——ASIN、店铺名、手机号、邮箱、路径、凭据。

数字确实想管，就写进 `terms.txt`，按词精确匹配（如某个具体金额）。

**你的业务词（品牌名、竞品名、MSKU 前缀等）走本地词表，不进仓库**：

```bash
# ~/.git-guard/terms.txt  每行一个词
某品牌名
某竞品名
HQY
```

### 放行

命中但确认无害，两种方式：

```bash
# ① 精确放行：写进 ~/.git-guard/allow.txt（正则，逐行）
^2\.某店铺-DE$          # 这个店铺名本身
^B0EXAMPLE0$            # 文档里的示例 ID

# ② 这次算了：--no-verify
git commit --no-verify
git push --no-verify
```

放行是**精确匹配**，不是模糊匹配：`allow` 里写了 `^2.某店铺-DE$`，独立的品牌词仍会被拦。

注意放行的对象是**命中值**：`本地路径` 抓到的命中值是用户名，不是整条路径，
所以写一条完整的 Windows 路径放行不生效，得直接放行那个用户名（`^USER$`）。

命中值默认打码（`手机号`/`邮箱`/`疑似凭据`/`私钥`/`自定义词` 显示成 `13***78 (len=11)`），
免得把要保护的东西又抄进 CI 日志一份；要看清全文加 `--show-values`：

```bash
python ~/.git-guard/scan.py --staged --show-values
```

### 边界

- **只拦 commit 和 push，不拦 `git add`**。add 只是暂存，不落历史。
- **找不到 python，或 `python` 是 Microsoft Store 的占位存根时，警告并放行**，不会把你锁死在外面。
- **编辑器/IDE 的 Git 面板同样触发**（VSCode 等底层调用同一个 `git` 二进制）。少数用 libgit2 的旧客户端可能忽略钩子。
- **`core.hooksPath` 会盖掉模板钩子**：husky / lefthook 项目不认 `init.templateDir`，得手动把 git-guard 接进它们的 hook 脚本。
- 扫描器异常（exit 2）按阻断处理，避免静默失效。

### 自检

```bash
python tests/run_tests.py        # 16 条回归用例，只用标准库
```

---

## 二、手动层：历史脱敏向导

已装 `git-filter-repo`：

```bash
pip install git-filter-repo
```

### 第 1 步：出草稿（只读，零风险）

```bash
cd <你的仓库>
python ~/.git-guard/sanitize.py suggest
```

生成 `map.suggested.txt`，自动给出店铺名 → `店铺A-DE`、ASIN → `B0ASIN0001`、邮箱 → `contact1@example.com` 的草稿映射，并列出**含敏感内容的二进制文档**（xlsx/docx 等）。

### 第 2 步：人工过一遍

打开草稿，改占位符名、删掉不该动的、补上漏掉的。格式：

```
原值==>占位符
某品牌名==>BRAND_A
B0EXAMPLE1==>ASIN_C1
regex:\bB0[A-Z0-9]{8}\b==>B0ASINREDACTED
```

**顺序有意义**：先长后短。`2.某店铺-DE` 要写在 `某店铺` 前面。

兜底那条正则别忘了留负向断言（`(?!ASIN\d{4,}\b|EXAMPLE\d*\b|XXXXXXXX\b)`）：
`filter-repo` 是**先跑全部字面量、再跑正则**，裸的 `\bB0[A-Z0-9]{8}\b` 会把上面刚生成的
`B0ASIN0001` 一起吞成 `B0ASINREDACTED`，几个不同 ASIN 抹成同一个，还不报错。

### 第 3 步：执行

```bash
python ~/.git-guard/sanitize.py apply map.suggested.txt
```

会先问一次（要打 `yes`），然后依次：

1. 体检：浅克隆直接拒（`--allow-shallow` 才放行）；已跟踪文件有改动就停；未跟踪文件只提示
2. `git bundle create guard-backup-<时间戳>.bundle --all` 备份到仓库**上一级目录**，并 `git bundle verify` 验一遍能读回
3. 调 `git filter-repo --replace-text --replace-message`
4. **自动复扫** `--all --reflog`，把残留值列出来（别在这里就 force push）
5. 打印后续手动的 force push 命令

### 第 4 步：强推

```bash
git remote add origin <仓库URL>    # filter-repo 会删掉 origin，防误推
git push --force --mirror origin
```

第 3 步末尾已经复扫过了；想再确认一次，用真·全历史（`--all` 等价于 `--all --reflog`，会一并看其它分支和 reflog）：

```bash
python ~/.git-guard/scan.py --all
```

### 二进制文档（重要）

`filter-repo` 的文本替换**跳过含 NUL 的 blob**，所以历史里的 xlsx/docx 不会被替换。`suggest` 会点名这些文件，处理方式：

```bash
# 把历史版本整个剔除，再把脱敏副本作为新提交加回
python ~/.git-guard/sanitize.py apply map.txt --drop docs/教程.xlsx
git checkout -- docs/教程.xlsx   # 或从别处拷脱敏版
git add docs/教程.xlsx && git commit -m "Add sanitized workbook"
```

---

## 三、必须知道的两件事

**1. 改历史会改掉所有 commit hash**

旧 hash 在重写后的仓库里 `fatal: Not a valid object name`。别人 clone 的副本要重新同步：

```bash
git fetch origin && git reset --hard origin/main
```

**2. 只管得住"新克隆"**

filter-repo 只能保证**从这次推送之后的所有 clone 干净**。已经扩散出去的副本不受控：

- 别人的 fork
- GitHub 页面缓存 / CDN
- 第三方镜像站、搜索引擎快照、skill 聚合站

如果敏感数据曾经公开过一段时间，**删库重建**比清洗更彻底。

---

## 四、目录

```
scan.py            扫描器（commit 暂存区 / 提交信息 / 待推提交 / 全历史）
sanitize.py        历史脱敏向导（suggest / apply）
pre-commit         钩子：调 scan.py --staged
commit-msg         钩子：调 scan.py --message
pre-push           钩子：调 scan.py --ranges-stdin
install.py         一键安装钩子到全局模板
tests/run_tests.py 回归用例（只用标准库）
BUGS.md            修过的漏检清单，含复现与用例对应关系
```

本地运行时文件（**不入库**）：

```
~/.git-guard/scan.py       扫描器副本，钩子调它
~/.git-guard/terms.txt     你的业务敏感词
~/.git-guard/allow.txt     允许的正则
```

## License

MIT（见 [LICENSE](LICENSE)）

---

## 五、实战中踩到的坑（都是真实发生的）

**1. 数字类噪音会让防护失效**

早期版本连金额一起拦，结果自己的技能正文里几十个 `€N,NNN` 全被拦，
每次提交都得 `--no-verify`。现在业务数字不报，只保留能定位到人的标识与联系方式。

真要管某个具体金额，加进 `terms.txt` 按词精确匹配，比宽泛的数字正则可靠。

**2. 手机号正则要避开 browse node**

Amazon 类目 ID 形如 `(16392411031)`，11 位、以 1 开头，跟中国手机号撞型。
当前规则用 `(?<![\w(])...(?![\w)])` 排除括号包裹的形态——
这类数字在统计报表里成片出现，不排除就没法用。

**3. 二进制文档必须单独处理**

`filter-repo` 跳过含 NUL 的 blob。历史里的 xlsx/docx **不会**被替换。
`sanitize.py suggest` 会点名它们，`apply --drop <path>` 剔除历史版本，
再把脱敏副本作为新提交加回。

**4. 历史里的旧提交比 HEAD 危险**

第一次清洗后 `git log` 看着干净，但 `--all` 一扩查就发现旧 blob 还在。
规则文件要**覆盖全部分类**（标识 + 金额 + 路径 + 消息），漏一类就漏一片。

**5. filter-repo 会删掉 origin**

这是防误推的设计，不是 bug。推之前 `git remote add origin <URL>`。

**6. 改写后所有 hash 变化**

其他机器：`git fetch && git reset --hard origin/main`。
别人 fork 的副本不受控，敏感数据若曾公开过，删库重建更彻底。

---

## 六、修过的漏检

上面这些坑之外，还有一批**静默漏检**是后来复现出来并修掉的：`--all` 只看 HEAD 导致的假清白、
UTF-16/GBK/OLE 三种编码整段失明、`git mv` 被判成 rename 时内容没扫、提交信息无人检查、
`sanitize suggest` 同站点只采第一个店铺。每一条都有复现步骤和对应的回归用例：

- 清单：[BUGS.md](BUGS.md)
- 用例：`python tests/run_tests.py`
