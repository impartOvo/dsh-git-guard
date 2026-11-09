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

1. 把 `scan.py` 复制到 `~/.git-guard/scan.py`
2. 把 `pre-commit` / `pre-push` 装到 `~/.git-templates/hooks/`
3. 设置 `git config --global init.templateDir ~/.git-templates`

**只对之后新建/新克隆的仓库自动生效。** 已有仓库补装：

```bash
cp ~/.git-templates/hooks/pre-commit .git/hooks/
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

### 边界

- **只拦 commit 和 push，不拦 `git add`**。add 只是暂存，不落历史。
- **找不到 python 时警告并放行**，不会把你锁死在外面。
- **编辑器/IDE 的 Git 面板同样触发**（VSCode 等底层调用同一个 `git` 二进制）。少数用 libgit2 的旧客户端可能忽略钩子。
- 扫描器异常（exit 2）按阻断处理，避免静默失效。

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

### 第 3 步：执行

```bash
python ~/.git-guard/sanitize.py apply map.suggested.txt
```

会先问一次（要打 `yes`），然后：

1. `git bundle create guard-backup-<时间戳>.bundle --all` 备份到仓库**上一级目录**
2. 调 `git filter-repo --replace-text --replace-message`
3. 打印后续手动的 force push 命令

### 第 4 步：强推并复扫

```bash
git remote add origin <仓库URL>    # filter-repo 会删掉 origin，防误推
git push --force --mirror origin
python ~/.git-guard/scan.py --all  # 复扫确认
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
scan.py            扫描器（commit 暂存区 / 待推提交 / 全历史）
sanitize.py        历史脱敏向导（suggest / apply）
pre-commit         钩子：调 scan.py --staged
pre-push           钩子：调 scan.py --ranges-stdin
install.py         一键安装钩子到全局模板
```

本地运行时文件（**不入库**）：

```
~/.git-guard/scan.py       扫描器副本，钩子调它
~/.git-guard/terms.txt     你的业务敏感词
~/.git-guard/allow.txt     允许的正则
```

## License

MIT

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
