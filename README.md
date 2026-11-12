# CANNJudge CLI

创建日期：2026-11-12。作者：ckyasb。
用途：独立使用的 CANNJudge 多文件提交、评测查询和匿名成绩记录工具。
新增摘要：提供通用使用文档、环境配置占位及凭据和运行记录的 Git 忽略规则。

使用已有网站会话读取模板并提交源码；成绩和名次使用独立的匿名连接读取公开排行榜。支持生成可审查的完整文件计划、保留原提交中的其他文件，以及本地追加保存提交 ID、分数、名次、查询时间和来源。

只依赖 Python 3.9+ 标准库，无需安装浏览器、第三方包或 NPU 环境。工具本身不编译或验证算子；请在提交前完成候选代码的精度和性能验证。

## 目录

```text
.
├── .env_example          # 环境变量占位，只有 Cookie 文件路径
├── .gitignore            # 排除真实配置、凭据和本地运行数据
├── README.md
├── reports/
│   └── .gitkeep          # 保留默认输出目录，实际运行记录不提交
└── tools/
    ├── cannjudge.py
    └── test_cannjudge.py
```

以下命令在仓库根目录执行，`python3` 指已有的 Python 3.9+ 环境。

```bash
git clone git@github.com:ckyasb/cannsubmit.git
cd cannsubmit
```

## 配置登录态

将已有 Cookie 导出保存到仓库之外的文件，并设置路径。不要将 Cookie 内容放进命令参数、Git 文件或示例配置。

```bash
cp .env_example .env
# 先编辑 .env，将 CANNJUDGE_COOKIE_FILE 改为实际导出文件的绝对路径。
set -a
. ./.env
set +a
```

`.env` 需要由当前 shell 显式加载，工具不会自动读取。也可直接 `export CANNJUDGE_COOKIE_FILE=/path/outside/repository/cannjudge-cookie.txt`，或在子命令之前传 `--cookie-file <文件路径>`。匿名 `score` 命令不需要登录，并忽略以上两种配置，不读取其指向的文件。

支持的已有导出格式：

- Netscape/curl Cookie 文件。
- 浏览器 JSON Cookie 数组，或带 `cookies` 数组的对象。
- 单行 Cookie 请求头，可带 `Cookie:` 前缀。
- 网站原始签名会话值，按 `cannjudge_auth` 名称加载。

浏览器导出只加载 `cannjudge.cn` 的 Cookie。提交前通过网站个人信息接口验证会话；会话值只在内存中使用，并对命令输出做脱敏。API 重定向被拒绝。匿名查分连接不保存或回传服务端的 `Set-Cookie`。

## 查看题目和文件

```bash
python3 tools/cannjudge.py problems
python3 tools/cannjudge.py files --problem dot
```

默认比赛为 `https://cannjudge.cn/public/asic-op-s10`。其他比赛可在子命令之前传 `--contest-url https://cannjudge.cn/public/<比赛名>`。`--problem` 接受题目的 name、canonical_name、数字 ID 或 24 位对象 ID，以 `problems` 输出为准。

## 生成和审查计划

将 `--source` 改为本次已验证的候选源码目录，避免误提交旧目录。使用尚不存在的输出路径：

```bash
python3 tools/cannjudge.py plan --problem dot --source /path/to/candidate \
  --diff --output reports/submission_plan.json
```

计划保存完整待提交 `files`、模板哈希、变更前后 SHA256 和可选 diff。生成计划不会提交到平台。默认以当前模板为基础，替换候选目录中存在的可编辑文件，保留缺失的模板文件，并包含规则允许的新增依赖。

只读模板文件不会进入提交内容。普通算子只能在模板已存在的 `op_host`、`op_kernel`、`op_api` 目录新增 `.h`、`.cpp`，最多 20 个；NPU kernel 模板遵循对应 `.asc`/`.h` 规则。以当前平台模板和实际校验为准。

基于旧提交，只替换指定文件：

```bash
python3 tools/cannjudge.py plan --problem dot --source /path/to/candidate \
  --from-submission <已有提交的24位对象ID> \
  --file op_kernel/dot.cpp --file op_host/dot_tiling.cpp \
  --diff --output reports/incremental_plan.json
```

未选中的文件保留旧提交内容。没有 `--from-submission` 时基础内容来自模板；浏览器 localStorage 草稿不会自动参与提交。

## 提交一次，继续查询同一个 ID

审查完整计划后执行。这里的 `commit` 指 CANNJudge 提交，不是 Git commit：

```bash
# Bash：避免重定向覆盖旧回执；为每次提交选择新的文件名。
set -o noclobber
python3 tools/cannjudge.py commit --plan reports/submission_plan.json --wait 120 \
  > reports/submission_receipt.json
cat reports/submission_receipt.json
```

提交前重新检查模板、可编辑标志及新增文件规则。成功后先将回执追加到本地历史，再查询评测和公开分数。输出包含 `submission_id`、`problem_id`、`user_id`、详情链接及 `score_snapshot`。省略 `--wait` 则提交后立即查分。

使用回执中的提交对象 ID 继续查询，无需再次提交：

```bash
python3 tools/cannjudge.py status --id <submission_id> --wait 120
```

`status` 需要原有权限读取评测详情；其分数来自另一个匿名连接。若详情缺少题目或用户 ID，保留评测状态并将分数标为 `unavailable`，再使用下面的显式 ID 查分。

## 匿名查分

```bash
python3 tools/cannjudge.py score --receipt reports/submission_receipt.json
```

也可显式提供三个 ID，无需会话文件：

```bash
python3 tools/cannjudge.py score \
  --problem-id <题目的24位对象ID> \
  --user-id <用户的24位对象ID> \
  --id <提交的24位对象ID>
```

`--id` 接受 24 位十六进制对象 ID，不是网页显示的数字提交编号。`--problem-id` 可从 `problems` 获取；三个对象 ID 均包含在新提交回执中。旧回执缺少字段时可用显式参数补全，字段冲突会被拒绝。`--receipt` 接受单个 JSON 对象，不能直接传整个 JSONL 历史文件。

查询只访问 `/api/problems/<problem_id>/ranking`，按用户 ID 定位并支持分页。该榜单通常只公开它采用的一次提交；最新提交未被采用时，无法通过榜单获得它的独立分数。

| 快照字段 | 含义 |
|---|---|
| `submission_id` | 本次请求查询的提交对象 ID |
| `submission_score` / `submission_rank` | 榜单采用本次提交时的实际分数/名次，否则为 `null` |
| `leaderboard` | 当前用户榜单采用的提交对象 ID、数字编号、分数、名次和状态 |
| `score_state` | `matched`、`ranking_uses_other_submission`、`not_ranked`、`unavailable` |
| `reason` | 未取得本次独立分数的原因；成功匹配为 `null` |
| `queried_at_utc` / `source_url` | 查询时间与公开数据来源 |
| `cookie_used` | 查分未使用 Cookie，固定为 `false` |

独立 `score` 命令直接返回快照；`commit` 和 `status` 将快照放在 `score_snapshot` 中。不要读取详情中的 `theory_score` 或将单个公开点的 `score` 当作算子总分；旧版 `status` 顶层可选的 `score` 已取消。

例如，以下使用虚构 ID 和示意分数，说明历史最好提交与本次提交的区别：

```json
{
  "submission_id": "333333333333333333333333",
  "submission_score": null,
  "submission_rank": null,
  "score_state": "ranking_uses_other_submission",
  "leaderboard": {
    "submission_id": "444444444444444444444444",
    "submission_number": 123456,
    "score": 75.5,
    "rank": 10
  },
  "cookie_used": false
}
```

这里的 75.5 分属于 `leaderboard.submission_id`，本次提交分数仍为 `null`。`null` 不表示零分或正确性失败；排行榜分数也可能随其他提交及 tbest 变化。

## 本地记录和路径

`commit`、`status`、`score` 默认追加保存到仓库的 `reports/cannjudge_scores.jsonl`。每行一条 JSON，不覆盖旧记录。`submission_receipt` 记录提交回执，`score` 记录分数快照；`status` 快照还包含数字提交编号及评测状态。

```bash
tail -n 5 reports/cannjudge_scores.jsonl
```

三个命令都支持 `--history <路径>`，父目录须存在。候选、计划、回执和自定义历史的相对路径以当前工作目录为准；默认历史位置以工具所在仓库为准。

Git 忽略 `.env`、凭据导出及 `reports/` 内的实际记录，仅保留 `.env_example` 和 `reports/.gitkeep`。计划可能包含待提交源码，回执和成绩包含账号/提交 ID，请将这些运行数据保留在被忽略的目录内。已有真实凭据没有打包到此仓库；忽略规则不能清除已进入 Git 历史的文件。

## 参数和错误处理

全局参数必须放在子命令之前。完整帮助：

```bash
python3 tools/cannjudge.py --help
python3 tools/cannjudge.py plan --help
python3 tools/cannjudge.py commit --help
python3 tools/cannjudge.py score --help
```

| 参数 | 适用范围 | 含义 |
|---|---|---|
| `--cookie-file` | 全局 | 登录态文件；独立 `score` 忽略此配置及对应环境变量 |
| `--contest-url` | 全局 | `problems`、`files`、`plan` 使用的比赛地址 |
| `--file` / `--from-submission` / `--diff` | `plan` | 指定替换文件、基础提交及差异输出 |
| `--wait` | `commit`、`status` | 最长轮询时间，默认 0 秒；每个 HTTP 请求仍受连接超时约束 |
| `--history` | `commit`、`status`、`score` | 追加式本地 JSONL 记录路径 |
| `--receipt` | `score` | 从单个回执 JSON 读取查询上下文 |

- 模板变化、只读文件或非法新增文件：按当前模板重新生成并审查计划。
- 401/403：检查会话有效期、绑定状态和比赛权限；匿名查分可独立执行。
- Waiting/Running/Unjudged：用原提交 ID 继续查询，不能据此判断正确性失败。
- POST 超时或响应没有 ID：服务端可能已接收，先核对网站记录，工具不会自动重试提交。
- `unavailable`：查看 `reason` 并稍后重试读取；不能补 0，也不要重发 POST。
- `history_error`：保留已返回的提交 ID，修复本地路径或磁盘问题，再查询并记录；非零退出码不代表已接收的提交失败。

普通成功查询退出码为 0，`unavailable` 也可能返回 0；自动化调用同时检查 `score_state`。工具错误或历史写入失败返回 1，中断返回 130；参数用法错误由 argparse 返回 2。标准输出为 JSON，命令行错误写入标准错误。重定向产生空文件时请检查错误信息，不能假定已有有效回执。

## 离线测试

```bash
python3 -m unittest discover -s tools -p test_cannjudge.py -v
```

测试使用虚构会话、本地 HTTP 服务及临时目录，不会创建真实 CANNJudge 提交。覆盖多文件保留、只读/越界保护、模板变化、会话脱敏、重定向阻断、POST 不重试、匿名分页和 Cookie 隔离、分数与提交 ID 的对应关系及追加式历史。可用 `CANNJUDGE_TEST_TMPDIR` 指定已有的可写临时目录。
