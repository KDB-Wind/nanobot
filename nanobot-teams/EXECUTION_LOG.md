# Nanobot Teams 执行日志(S0–S6 长程运行)

> 开始:2026-11-08 23:33(Asia/Shanghai)。执行者:ZCode(GLM-5.3),全程 poteto-mode。
> 决策链明细见 `decisions.tsv`;未解决问题见 `BLOCKERS.md`。
> 边界:只改 `nanobot-teams/`;Nanobot 核心零改动;只做本地提交。

## 状态总览

| 阶段 | 内容 | 状态 | 提交 |
| --- | --- | --- | --- |
| S0 | MVP 收尾:Unix 复跑 + 阶段 6 发布准备 | 完成 | `361d9997f`、`289a13659` |
| S1 | 多 Bot Registry(单跳、多目标) | 完成 | `ef5af2e28` |
| S2 | 多跳与回复路由 | 完成 | `28a5cb476` |
| S3 | Bot 生命周期工具 | 完成 | `b27a0acdf` |
| S4 | 任务账本与可观测性 | 完成 | `6f946043d` |
| S5 | 管理与策略 | 完成 | `7ad04c42a`+`0073794da`+`5fddcb87d` |
| S6 | 硬化与发布 | 完成 | `c2dda9b16`+`44209624b`+`79b9d406a`+`d8967f1b0`+`8a00fdd` |

## S0:MVP 收尾(2026-11-08 完成)

### 做了什么

1. Windows 基线:`pytest -q` 88 通过,`ruff check src tests acceptance` 无告警。
2. Unix 环境:WSL2 Ubuntu 22.04(内核 6.18.33)从主仓库克隆 `feat/nanobot-teams-mvp`,
   `uv venv --python 3.11` 建 `.venv-nanobot`(装 `.[api]`)与 `nanobot-teams/.venv`
   (装本包 + pytest),`~/.nanobot/config.json` 文件级拷贝(不回显)。
3. WSL NAT 无直连外网:写 `acceptance/relay.py`(纯字节转发,绑 WSL 网关
   `172.25.240.1:17897` → 宿主代理 `127.0.0.1:7897`),WSL 侧 `http_proxy` 指向中继,
   `no_proxy=127.0.0.1,localhost` 保护本机健康检查。不改用户代理软件配置。
4. `run_e2e.py` 按 `os.name` 选解释器路径(Windows `.venv/Scripts`、POSIX `.venv/bin`),
   其余启停逻辑本就跨平台。
5. 阶段 6 收尾:`test_packaging.py` 新增源码静态扫描(禁止文件系统写调用),
   README 增加支持矩阵,acceptance/README 增加 Unix 章节与实测记录。

### 验收记录

| 命令 | 平台 | 输出摘要 |
| --- | --- | --- |
| `.venv/Scripts/python.exe -m pytest -q` | Windows | `88 passed in 2.84s` |
| `ruff check src tests acceptance` | Windows | `All checks passed!` |
| `.venv/bin/python -m pytest -q` | Ubuntu/WSL2 | `88 passed in 2.31s` |
| `.venv/bin/python acceptance/run_e2e.py all` | Ubuntu/WSL2 | 6/6 PASS(research_direct、delegate_basic、session_persistence、long_timeout elapsed=38.2s、unknown_block、plugin_manifest) |

Windows 侧 6 场景 + WebUI 场景在 MVP 验收(commit `9c2e90082`)已通过;最终验收时统一复跑。

### 已知限制

- WSL 出网依赖宿主侧中继进程(运行期常驻,随会话停止);这是本机环境适配,不是产品依赖。
- WebUI 验收只在 Windows 跑过(需要 Edge);Unix 无浏览器场景。
- Unix 平台为 WSL2,非裸机/容器 Linux;信号与路径行为以 WSL2 为准。

### 下一步

S1:多 Bot Registry。bots[] 增加 `capabilities[]`、`allowedCallers[]`、`modelPreset`,
caller 身份改为每实例 `--caller` 参数指定,`list_bots` 按授权过滤;三 bot 验收。

## S1:多 Bot Registry(2026-11-09 完成)

### 做了什么

1. Registry schema v2:授权从"全局 caller + allowed_targets"改为每目标的
   `allowed_callers` 列表;bots 增加 `capabilities[]`(短 token,≤8 个)、
   `model_preset`(advisory,经 list_bots 暴露)。JSON 字段沿用 v1 的 snake_case
   风格(ROADMAP 里的 camelCase 是描述性命名)。
2. 实例身份:`--caller <bot-id>` 命令行参数(受信控制面,非模型输入)。v2 必填;
   v1 默认取 `caller_bot_id` 且 --caller 必须与之一致(防止歧义)。
3. v1 归一化:v1 文件继续可加载,allowed target 归一为 `allowed_callers=[caller]`,
   固定 caller 成为默认身份。两个 schema 归一到同一 TeamConfig 模型。
4. 图校验:allowed_callers 引用的 bot 必须存在、不得自引用、不得重复;
   有 allowed_callers 的 bot 必须带 endpoint+api_key_env,没有的禁止携带。
   密钥按 caller 可达目标解析(中间跳 bot 只拿它够得着的 key)。
5. 验收场景:`multi_bot`(三个真实实例,planner 同轮委派 research 与 writer,
   双 nonce 回传)、`unauthorized_pair`(真实 MCP 进程 --caller writer,
   list_bots 为空、delegate 被拒、stub HTTP 计 0 次命中)。

### 验收记录

| 命令 | 平台 | 输出摘要 |
| --- | --- | --- |
| `pytest -q` | Windows / Ubuntu | `105 passed`(两侧一致;新增 16 个 v2/授权测试) |
| `ruff check src tests acceptance` | Windows | `All checks passed!` |
| `run_e2e.py unauthorized_pair` | Windows / Ubuntu | PASS |
| `run_e2e.py multi_bot` | Windows / Ubuntu | PASS(三实例、两授权对、真实模型) |
| `run_e2e.py delegate_basic`(v1 回归) | Ubuntu | PASS(v1 registry 不带 --caller 仍工作) |

### 已知限制

- `model_preset` 只是元数据,不驱动目标实例的模型选择(实例配置仍由用户管理)。
- 授权图是静态的:授权变更需要改 registry 并重启相关 MCP 实例。
- 会话 ID 公式含 schema_version:v1 升 v2 后同一团队线程映射到新会话
  (旧会话历史留在原 Nanobot 实例中,不丢失)。

### 下一步

S2:多跳与回复路由。信封(task/parent/root id、hop、deadline_remaining)、
每跳预算扣减、环路检测、A→B→C 验收。

## S2:多跳与回复路由(2026-11-09 完成)

### 做了什么

1. 信封(envelope.py):五元组 `root_task_id/parent_task_id/hop/
   deadline_remaining_s/chain` + 任务 id。每次委派的任务文本顶部嵌入
   `[teams-envelope]` 机器可读块,内容是"下一跳"视角(root 不变、
   parent=本次任务、hop+1、deadline×0.9、chain 追加目标)。
2. 中跳传播:中间 bot 的 Skill 指导它把块内 root/hop/deadline/chain 原样
   复制进 delegate_task 参数,parent 填块内 task_id。MCP 端不信任这些值,
   逐项校验:uuid 形状、hop∈[2, max_hops]、chain 全员在册且以 caller 结尾、
   len(chain)==hop、目标不在 chain(环路→loop_detected)、deadline>0
   (≤0→budget_exhausted)、部分信封→envelope_invalid。
3. 预算扣减:本跳等待上限 = min(total_timeout, deadline);子跳预算=父×0.9
   (单调递减,永不放大)。根调用(无信封参数)行为与 MVP 完全兼容。
4. 结果 JSON 增加 root_task_id 与 hop;日志行增加 root/hop。

### 验收记录

| 命令 | 平台 | 输出摘要 |
| --- | --- | --- |
| `pytest -q` | Windows / Ubuntu | `116 passed`(新增 11 个信封测试) |
| `ruff check src tests acceptance` | Windows | `All checks passed!` |
| `run_e2e.py chain_abc` | Windows / Ubuntu | PASS(planner→mid→research 三真实实例,mid 的模型从信封块续传,nonce 沿链回传) |
| `run_e2e.py chain_loop` | Windows / Ubuntu | PASS(loop_detected,stub HTTP 0 命中) |
| `run_e2e.py chain_budget` | Windows / Ubuntu | PASS(budget_exhausted 不发 HTTP;0.5s 预算 + 3s stub → timed_out 而非 completed) |

### 已知限制

- 信封值经中跳模型转写,属结构化但不可信输入;MCP 只能强制上界与形状,
  无法验证"这就是上游真实生成的值"(无账本前无跨实例对账)。
- deadline 传递是尽力而为的上界:中跳模型上报的剩余时间可能偏大,
  但每跳 ×0.9 且被本地 total_timeout 钳制,不会放大。
- 排障发现并修复:e2h 场景 planner 的 MCP env 漏配中跳目标 key 时,
  MCP 子进程启动即退、工具消失,模型侧表现为"没有该工具"。已在
  planner_config 增加 extra_mcp_env 参数修正。

### 下一步

S3:Bot 生命周期工具。`nanobot-teams bot init` 生成骨架与清单,
`nanobot-teams check` 校验 registry/端点/密钥/preset/端口。

## S3:Bot 生命周期工具(2026-11-09 完成)

### 做了什么

1. `nanobot-teams bot init <id>`:生成 `<home>/config.json`(loopback API、
   preset、workspace 受限、禁 exec)+ `<home>/workspace`,向 schema 2 registry
   追加该 bot(endpoint/api_key_env/allowed_callers),打印操作清单(密钥变量名、
   serve 命令、MCP 注册提示)。拒绝路径全部零写入:重复 id、未知 caller、
   端口被占、更新后的 registry 无法通过 loader 校验。
2. `nanobot-teams check --config ...`:逐行输出 `[severity] code: message`,
   error 级存在时退出 1。检查项:registry 可加载、每个目标的 api_key_env
   已设置、端点 socket 探测(up/down 均为 warn,停机不是错误)、
   model_preset 在 ~/.nanobot/config.json 中存在、nanobot CLI 版本与
   已测基线 0.4.1 的偏差(warn)。`--caller` 限定视角,`--no-probe` 跳过探测。
3. CLI 路由:新增 `serve`/`bot init`/`check` 子命令,裸 `--config` 路径保持
   原样(MCP 注册无需改动)。lifecycle.py 成为 src 中唯一允许文件写入的模块
   (操作者指定路径的 CLI 生成器),静态扫描测试显式豁免并注明原因。

### 验收记录

| 命令 | 平台 | 输出摘要 |
| --- | --- | --- |
| `pytest -q` | Windows / Ubuntu | `126 passed`(新增 10 个 lifecycle 测试) |
| `ruff check src tests acceptance` | Windows | `All checks passed!` |
| `run_e2e.py bot_init_live` | Windows / Ubuntu | PASS(CLI init → 生成配置真实启动 → check 退出 0 → planner 委派新 bot → nonce 回传) |
| `run_e2e.py check_errors` | Windows / Ubuntu | PASS(check 退出 1,报 missing_env 与 unknown_preset) |

### 已知限制

- init 生成的配置是保守基线(restrictToWorkspace、禁 exec、loopback);
  用户仍需按需调整模型参数与工具面。
- check 的端点探测只证明"有/无进程在监听",不证明服务语义正确。
- 版本核对比较字符串与基线 0.4.1;nightly 构建会触发 warn。

### 下一步

S4:任务账本与可观测性。Teams 自有 SQLite(仅本地观察),list_tasks/get_task
工具,结构化日志字段;重启后可查历史,账本不含提示词与密钥。

## S4:任务账本与可观测性(2026-11-09 完成)

### 做了什么

1. `ledger.py`:单一 SQLite 文件(`--ledger <path>` 显式开启,缺省零写入,
   现有 MCP 注册不受影响)。每条到达 transport 的委派记一行:
   task/root/parent id、caller、target、hop、status、error_code、truncated、
   http_sent、请求/响应/结果字节数、起止时间与耗时。WAL 模式;
   写失败只记日志,绝不影响委派本身。
2. 新增只读 MCP 工具 `list_tasks`(limit/target/status 过滤)与 `get_task`,
   每行 JSON 携带 observation 注记("本地观察,不代表远端完成")。
3. 结构化日志字段补齐:task_id、root、caller、target、hop、duration_ms、
   status、error_code、request/response/result 字节数。
4. 隐私边界:任务文本、回答内容、密钥一律不入库(e2e 直接扫描 sqlite
   文件字节断言);本地拒绝(未成任务)不记录。

### 验收记录

| 命令 | 平台 | 输出摘要 |
| --- | --- | --- |
| `pytest -q` | Windows / Ubuntu | `133 passed`(新增 7 个账本测试,stdio 测试扩至 4 工具) |
| `ruff check src tests acceptance` | Windows | `All checks passed!` |
| `run_e2e.py ledger_restart` | Windows / Ubuntu | PASS(两个独立 MCP 进程生命周期;重启后 list_tasks/get_task 可查;DB 字节级无任务文本/回答/密钥) |

### 已知限制

- 账本只在启用 `--ledger` 的实例上累积;每个 MCP 实例一个文件,
  跨实例聚合属 S5 audit 的读取范围(多文件并列)。
- http_sent 仅在"收到 HTTP 响应"时为 True;超时/传输错误的行显示 False,
   含义是"无证据表明请求到达",不是"确认未到达"。

### 下一步

S5:管理与策略。CLI graph/budget/audit;每 bot、每链路的调用预算
(次数、字节、时间);超预算明确拒绝。

## S5:管理与策略(2026-11-09 完成)

### 做了什么

1. 预算(schema 2 新增 `budgets` 段):按 caller 配 `delegations` / `result_bytes` /
   `chain_seconds` 三项可选上限。强制力来自账本累计(可跨重启);配置了
   预算但没有 --ledger 的实例拒绝一切委派(不猜)。超限本地拒绝,
   错误码 `budget_exceeded`,不发 HTTP。
2. CLI 只读视图:`graph`(授权图文本 + mermaid)、`budget`(各 bot 用量
   used/limit,超限标 OVER)、`audit`(跨多个账本文件的行级审计 +
   状态汇总,支持 target/status/limit 过滤)。
3. 只读 Web 页面:按 ROADMAP"可选"条款明确不做,CLI 覆盖验收语义;
   决策记录于 decisions.tsv。

### 验收记录

| 命令 | 平台 | 输出摘要 |
| --- | --- | --- |
| `pytest -q` | Windows / Ubuntu | `141 passed`(新增 8 个策略测试) |
| `ruff check src tests acceptance` | Windows | `All checks passed!` |
| `run_e2e.py budget_reject` | Windows / Ubuntu | PASS(上限 2:第 1、2 次 completed,第 3 次 budget_exceeded,stub 命中恰 2 次;graph/budget/audit CLI 输出断言通过) |
| `run_e2e.py ledger_restart`(回归) | Windows / Ubuntu | PASS |

### 已知限制

- 预算按 caller 计,不按 (caller,target) 对;配图里的"每链路"预算需
  账本按 target 聚合的扩展,当前 audit --target 可人工查看。
- 预算检查点在 busy/blocked 之后;同一进程内串行无竞态,多实例并行
  (同一账本文件)不做跨进程锁,与 ROADMAP"不做跨进程幂等"一致。
- 修复两处自身缺陷:str.replace 误触静态写扫描(改用 str.translate 保持
  扫描严格);e2e 场景残留在 .e2e 根下的账本文件未随 reset 清理(场景
  开头显式删除)。

### 下一步

S6:硬化与发布。契约回归(mock+真实基线)、版本矩阵、wheel 构建、
全新环境安装/升级/卸载、文档"已实现/已测试/不支持"三分。

## S6:硬化与发布(2026-11-09 完成)

### 做了什么

1. 版本 0.1.0 → 0.2.0(pyproject + `__init__`)。
2. wheel 构建与全新环境验收(`packaging_fresh` 场景):build 出 wheel →
   干净 venv 安装(依赖从 PyPI 解析)→ 版本断言 → 从新环境起 stdio MCP
   完成一次真实委派 → pip 卸载后 import 失败断言。升级语义 = 覆盖安装,
   不触碰任何 Nanobot home(包对 Nanobot 数据零写入,静态扫描+场景双证)。
3. README:新增"Implemented, tested, unsupported"三态矩阵(逐能力标注);
   Security boundaries 扩充信封不可信、账本内容策略、预算语义、
   lifecycle CLI 写入范围;支持矩阵记录包版本 0.2.0。
4. CI 工作流(`.github/workflows/ci.yml`,ubuntu+windows 矩阵跑 ruff+pytest)
   作为交付物入库;仓库未发布,workflow 未执行,如实记录。
5. 密钥审计:`git grep` 全仓库扫描模式(api_key/token/secret/password 赋值),
   无命中;示例与文档只含 `${ENV}` 引用。

### 验收记录

| 命令 | 平台 | 输出摘要 |
| --- | --- | --- |
| `pytest -q` | Windows | `141 passed` |
| `ruff check src tests acceptance` | Windows | `All checks passed!` |
| `run_e2e.py packaging_fresh` | Windows | PASS(wheel→干净 venv→委派→卸载全链路) |
| `run_e2e.py all` | Windows / Ubuntu | 见下方"最终验收" |
| `run_webui.py` | Windows | 见下方"最终验收" |

### 已知限制

- CI workflow 提供但未运行(仓库本地、未发布)。
- WebUI 验收场景未在 Unix 复跑(需要 Edge);服务端全场景两平台覆盖。
- wheel 发布(PyPI 上传)与包名可用性核验按用户授权另行执行。

## 最终验收(2026-11-09,提交 `8a00fdd` 树上)

| 项 | 平台 | 结果 |
| --- | --- | --- |
| `pytest -q` | Windows | `145 passed` |
| `pytest -q` | Ubuntu/WSL2 | `145 passed` |
| `ruff check src tests acceptance` | Windows | `All checks passed!` |
| `run_e2e.py all`(16 场景) | Windows | 16/16 PASS(long_timeout elapsed=38.6s) |
| `run_e2e.py all`(16 场景) | Ubuntu/WSL2 | 16/16 PASS(long_timeout elapsed=41.1s;packaging_fresh 首跑缺 build 模块,装入后单独复跑 PASS,plugin_manifest 复跑 PASS) |
| `run_webui.py` | Windows | PASS(plugin 在 Apps 启用,nonce 经 planner 回传) |

审计与修复闭环:交叉评审 10 项发现,3 项代码缺陷(NaN 信封穿透、预算
fail-open、INSERT OR REPLACE 违背 append-only)已修复并带回归测试
(`79b9d406a`);7 项记录/权衡项补入 decisions.tsv 与本日志。套件自身的
三处不稳(stub 残留监听、双平台并行负载下的时序、端口 TIME_WAIT 竞态)
在 `d8967f1b0` 与 `8a00fdd` 修复:stub 专用 8906 端口、finally 中
server_close、SO_REUSEADDR 探测 + 90s 上限、chain_budget 时间参数与
负载解耦。

## 结束状态

S0–S6 全部交付。改动全部位于 `nanobot-teams/`(基线 `9c2e90082` 以来
`git diff -- ':!nanobot-teams'` 为空)。Nanobot 核心零改动、零私有导入、
零私有文件读写(静态扫描测试持续生效)。仅本地提交,未 push、未打 tag。
`BLOCKERS.md` 记录 4 项未验证子项(CI 未运行、WebUI 未在 Unix 复跑、
只读页面按可选项未交付、wheel 未发布)。
