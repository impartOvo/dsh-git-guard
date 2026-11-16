# Changelog

版本号跟 `scan.py --version` / `sanitize.py --version` 一致。

## 0.3.0

这一版把"能拦"往"拦得住、绕不过、可维护"推了一轮。

### 新增检查

- **Office 元数据**：`docProps/core.xml` 的 `dc:creator` / `cp:lastModifiedBy`、
  `app.xml` 的 `Company`。Office 默认值（`Microsoft Office User` 等）不算命中。
- **凭据形态**：AWS `AKIA…`、GitHub `ghp_…`、`sk-…`、Slack `xox…-`、`AIza…`、
  Amazon SP-API `Atzr|…`、JWT（`eyJ…`）、带用户名密码的 URL。
- **身份证**：算 mod 11-2 校验位，格式对但校验位不对的不报。
- **银行卡**：走 Luhn，过不了的当业务数字放行。
- **Seller ID / Merchant Token**：只在有标签（`seller_id:` 之类）时才算。
- **子模块地址**：`.gitmodules` 里指向内网（`localhost` / RFC1918 / `*.internal` 等）
  或带凭据的 URL。
- **分支名 / tag 名**：pre-push 会把 ref 名也扫一遍。

### 不再"一上来就全红"

- **存量基线**：`scan.py --all --write-baseline` 把已有命中按「种类 + 值哈希」记进
  `.git/guard-baseline.txt`。之后这些值只提示、不阻断，新增值照拦。
  基线里只有哈希，没有明文；`sanitize apply` 重写历史后会自动清掉它。
- **按路径豁免**：`path-allow: vendor/**` 让整类目录不参与扫描。

### 绕过这件事有痕迹了

- **post-commit 遥测**：提交后按 `HEAD~1..HEAD` 复核一遍。命中说明 pre-commit 被跳过，
  写进 `~/.git-guard/bypass.log` 并大声提示——不阻断，但"习惯性 `--no-verify`"不再无声。
- **服务端兜底**：`server/pre-receive`，装在裸库上，客户端关钩子也绕不过去。
- **CI**：`.github/workflows/guard.yml`，三平台 × 两版本跑用例，另有一个本仓库全历史自检。

### 配置

- `~/.git-guard/{terms,allow}.txt`（全局，老格式仍然认）之上，加了两层仓库级配置：
  - `<repo>/.git-guard.conf` —— 可入库，只放非敏感项
  - `<repo>/.git/guard.conf` —— 本地，可以放业务词，永不入库
- 键：`term` / `allow` / `path-allow` / `disable` / `max-blob` / `baseline`。
- `disable: 银行卡` 这类开关就是给"数字密集的报表仓库"准备的退路。

### 性能与覆盖

- 大对象（>2MB）改走**可打印串抽取**（ASCII 段 + UTF-16 段），不再整份解码：
  100MB 补齐文件从 16s 降到 1-2s，而且**中间段也能覆盖**了（之前只有头尾）。
- `filter-repo` 替换不动的文件（含 NUL 的二进制、UTF-16 文本、Office 元数据）
  现在由 `suggest` 提前点名，并说清只能 `--drop` 或另存为 UTF-8。
- `suggest` 增加身份证 / 银行卡 / Seller ID 映射；
  和 `scan` 共用同一套解码器与正则（此前各抄一份，导致草稿漏项）。

### 版本与分发

- `install.py --version`、`sanitize.py --version`。
- `.pre-commit-hooks.yaml`：可以用 pre-commit 框架接入，不必改全局配置。
- 不提供 pipx 打包：`scan` / `sanitize` 是顶层模块名，装进 site-packages 会污染命名空间，
  代价大于收益。要单命令入口就用 clone + `python install.py`。

## 0.2.0

修掉一批**静默漏检**，详见 [BUGS.md](BUGS.md)。

- `--all` 走 `--all --reflog`（此前只看 HEAD，复扫给的是假清白）
- 按证据判定编码：UTF-16LE / OLE / GBK 不再整段失明
- `git mv`（判成 rename）不再漏扫内容
- 新增 `commit-msg` 钩子与 `--message`
- `cat-file --batch` 批读（802 对象 118.5s → 0.44s）
- 命中值默认打码，`--show-values` 看全文
- `suggest` 采集同站点的每个店铺；占位符单射；兜底正则不再吃掉占位符
- `sanitize`：`bundle verify`、浅克隆/脏工作区拒跑、重写后自动复扫

## 0.1.0

- 提交/推送前扫描 + 历史脱敏向导（`scan.py` / `sanitize.py` / `pre-commit` / `pre-push` / `install.py`）
