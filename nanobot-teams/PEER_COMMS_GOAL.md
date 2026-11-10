# Nanobot Teams 多 Bot 互通与可观测性长程目标

日期：2026-11-10。状态：执行交接，尚未启动。仓库：`C:\Users\KDB\orca\workspaces\nanobot\nanobot-teams-mvp`，分支 `feat/nanobot-teams-mvp`，基线 `db8e9ba56`。

配套文档：[异步工作台架构](nanobot-teams/ASYNC_WORKBENCH_ARCHITECTURE.md)、[实现方案](nanobot-teams/ASYNC_WORKBENCH_IMPLEMENTATION_PLAN.md)、[修复状态](nanobot-teams/REPAIR_STATUS.md)、[阻塞清单](nanobot-teams/BLOCKERS.md)、[路线图](nanobot-teams/ROADMAP.md)。冲突时：安全边界与公开协议以架构文档为准；本轮新增范围以本文为准。

## 1. 目标

让任意两个已注册、已授权的 Bot 在不共享 home、不伪造身份、不修改 Nanobot 核心的前提下，异步派发任务并收到结果。用户只与一个秘书长 Bot 对话；秘书长向 worker 派活；worker 之间可以互相派活；worker 只向秘书长或彼此汇报，不直接进入用户的会话。工作台能看到每个 Bot 的用量与缓存命中率。

交付后，一个四 Bot 拓扑（秘书长 + 三个 worker）应当能在一台机器上真实跑通：秘书长派给研究员，研究员转给写作者，写作者回复研究员，研究员汇总给秘书长，秘书长向用户汇报。中途杀掉协调器或任一 worker，重启后不重复、不丢单。

同时收尾 0.5 工作台的遗留交付项：过期 wheel、未提交文档、开放 PR、独立仓库同步、双平台 CI、文档漂移、已知不稳定测试。范围以第 3 节审计出的 backlog 为准，不做无限润色。

## 2. 非目标

- 不做通用群聊、自由文本 socket、跨机器部署、多租户、Grok Bot 克隆。
- 不重写 coordinator 的持久任务模型；复用现有 run、inbox、receipt 协议。
- 不为通过测试修改断言、mock 掉真实进程、放宽安全边界或降低验收标准。
- 不把 worker 之间做成无授权的对等网络；默认拒绝，授权显式。

## 3. 现状与差距

以下行号以 `db8e9ba56` 为准，执行时以实际代码为准。

- 协调器只有单个 `PlannerBinding`（`coordinator/config.py:29`），角色只有 `planner` 与 `operator`（`coordinator/http_api.py:199`）。
- 托管 Bot 的配置写死 `mcpServers: {}` 与 `channels: {}`（`lifecycle.py:147-148`），没有发起任务或接收投递的客户端。
- `auto_resume` 需要宿主铸造的 `reply_capability`（`async_mcp.py:346`）；铸造入口是通用的（`gateway_runtime.py:153, 639-641`），按 `runs_cfg.principal_id` 生成，不硬编码 planner。
- 每个 Bot 的用量库在 `<home>/llm_usage.sqlite3`，表 `llm_calls`，现有命中率定义在 `llm_usage/store.py:73-77, 510-521`。
- `serve` 提供 `/v1/agent-runs`（`cli/commands.py:415`），`gateway` 提供 inbox sidecar（`gateway_runtime.py:632`）。一个 Bot 同时收发，可能需要两个进程或一处宿主改动。这是本轮第一个必须用探针回答的问题，不能靠推断。
- 工作树有未提交的文档与证据；`dist/nanobot_teams-0.5.0-py3-none-any.whl` 构建于修复之前；PR #17 仍开放；独立仓库需要再次 `git subtree split` 同步。
- `tests/agent` 的 10 个 MCP fake session 失败是环境基线，与本轮无关，不要修。

## 4. 领域契约

实现可以选择不同的类与表，但以下不变量不能变。

**I1 身份独立。** 每个 Bot 有稳定的 `bot_id` 与协调器可验证的凭据。身份来自认证，不来自调用方自报的 header 或请求体。凭据只存哈希或环境引用，不进日志、不进 UI。

**I2 接收需要绑定。** 一个 Bot 只有配置了 inbox URL、inbox key 引用和 `principal_id`，才能接收异步结果。capability 由接收方宿主铸造，协调器只转发，不能代铸。

**I3 发送需要授权边。** planner 到已启用 Bot 的边保持现状（注册即授权）；worker 之间的边显式配置，默认拒绝。同步模式的 `allowed_callers` 不复用来做异步授权；需要迁移就写迁移，不混用语义。

**I4 先持久再发送。** 对端 HTTP 之前，任务行、正文摘要、operation key、sender、recipient、conversation root、hop 已落库。未知受理按原 key 与正文对账，不换 key 重做。

**I5 结果只回发起方。** 结果投递到发起方自己的 inbox，使用发起方宿主铸造的 capability。任何 Bot 不能向另一个 Bot 的 inbox 投递自己没有铸造的 capability。

**I6 环路与放大有上限。** 每个 conversation root 有 `max_hops`（默认 4）、每对 Bot 的单位时间消息上限、扇出上限、并发上限。超限是类型化拒绝，不是静默丢弃或无限排队。

**I7 用户入口唯一。** 只有秘书长（planner principal）绑定人的会话。worker 之间的消息不会自动注入用户会话；秘书长选择汇总时才产生用户可见轮次。

**I8 重启收敛。** 协调器或 Bot 在任意点崩溃，重启后收敛到同一终态。重复投递幂等；已完成的 receipt 不重复插入历史；失去所有者的待处理工作保留可查，不静默重跑。

**I9 宿主边界。** 不改 `nanobot/`，除非探针证明缺少必要原语。必须改时写 ADR、默认关闭、向后兼容、单独提交，并把改动限制在最小入口。

**I10 可观测但不泄露。** 事件记录 sender、recipient、task、hop、状态与错误码，不记录正文。用量统计只聚合 token 数，不读 prompt 或回复内容。

## 5. 缓存命中率契约

**定义。** 对每个 Bot、每个模型、每个时间窗：

- `observed_input_tokens = sum(input_tokens) where cache_read_tokens is not null`
- `cache_read_rate = sum(cache_read_tokens) / observed_input_tokens`
- `coverage = observed_input_tokens / sum(input_tokens)`
- 同时给出 `requests`、`input_tokens`、`cache_read_tokens`、`cache_write_tokens`。

分母是**报告了缓存字段的输入 token**，不是总 token（总 token 含输出），不是请求数。没有缓存字段的调用不进入分母。缺失数据是 `null`，不是 0%。`coverage` 用来说明这个比率覆盖了多少输入。

**来源。** `<bot_homes_root>/<bot_id>/llm_usage.sqlite3` 的 `llm_calls` 表，schema `user_version=1`。只读连接，`PRAGMA query_only=ON`，有 busy timeout。文件缺失、表缺失、版本未知时返回 `available: false` 与原因，不猜、不补零。

**接口。** `GET /v1/teams/usage?window=7d|30d|all` 返回全部 Bot；`GET /v1/teams/bots/{bot_id}/usage` 返回单个。响应包含 `bot_id`、`display_name`、`window`、`available`、`reason`、`totals`、`models[]`。金额与凭据不出现。

**界面。** 工作台每个 Bot 一行：名称、模型、请求数、输入 token、缓存读取、命中率、覆盖率。命中率为 `null` 时显示“未报告”，并给出原因，不显示 0%。表头或 tooltip 写明分母。

**测试。** 用固定 fixture 库断言精确值：100 输入 + 60 缓存、50 输入 + NULL，期望 60%、覆盖率 100/150。再覆盖多模型拆分、时区日界、缺失库、未知 schema。真实探针至少在一个报告缓存的 provider 上重复稳定前缀，确认 `cache_read_tokens > 0`；provider 不报告时记录事实，不伪造。

## 6. 阶段

每个阶段是一个可验证单元：先有失败证据，再实现，再复验，再提交。上一阶段没有 verified，不进入下一阶段；独立工作可以并行，但验收顺序不变。

### P0 基线、审计与恢复点

读取根 `AGENTS.md`、`.agent/*`、第 1 节列出的 Teams 文档。记录 `git status --short`、HEAD、tracked diff 摘要、未跟踪文件清单，复制到工作区外的备份目录并校验摘要。运行基线测试，记录命令、退出码、通过/失败/跳过数与已知失败。产出 `nanobot-teams/PEER_COMMS_BACKLOG.md`：每项含 id、证据、严重性、决定（do/defer/reject）、理由。至少覆盖第 3 节的遗留项、`ROADMAP.md` 未完成项、`BLOCKERS.md` 未验证项、PR #16/#17 的非阻断意见。

把本文复制为 `nanobot-teams/PEER_COMMS_GOAL.md`，作为本轮第一个文档提交，后续阶段以仓库内副本为准。

完成信号：基线表、backlog、恢复点三者齐全；没有修改产品代码。

### P1 设计与威胁模型

先建模再写代码。领域对象至少包括 `Principal`、`BotBinding`、`PeerEdge`、`ConversationRoot`、`HopBudget`、`PeerTask`、`DeliveryReceipt`。比较至少三种方案：每 Bot inbox 绑定 + runs 协议、协调器代持 standing capability、宿主级消息总线。写出选择与拒绝理由。威胁模型覆盖伪造发送方、capability 盗用、SSRF 到 Bot 端点、peer 消息中的提示注入、跨 Bot 数据泄露、环路放大、钱包耗尽。

必须回答第 3 节的运行时问题：一个 Bot 同时接收 inbox 与执行 runs，是单进程、双进程，还是需要一处宿主改动。用最小探针回答，不靠推断。

完成信号：`nanobot-teams/PEER_COMMS_DESIGN.md` 含不变量、方案对比、威胁模型、失败探针计划；探针问题有结论或明确的待验证实验。

### P2 失败探针（先红）

新增 `nanobot-teams/acceptance/peer_comms_probe.py`，启动真实四 Bot 拓扑（秘书长 + 三个 worker），断言：A 能给 B 派任务并收到回复；无授权边的 B→C 被拒绝；第五跳被 hop guard 拒绝；协调器在投递中途被杀后重启不重复、不丢单；事件里有 sender/recipient/task/hop。当前代码必须在第一条断言失败，记录错误码与日志。假执行器只能用于控制崩溃点，不能替代真实进程。

完成信号：探针红，失败证据落盘；探针本身可重复运行。

### P3 身份、绑定与迁移

协调器从单个 `PlannerBinding` 改为绑定表，优先在现有 `bots` 表上加列（inbox URL、principal、inbox secret 引用、role），不新建平行的 Bot 注册表；保留 planner 行；现有数据库打开后行为不变。新增 per-Bot 协调器凭据，作用域至少区分 `submit`、`receive`、`admin`；operator 仍只用于管理。`principal_for` 改为查表。迁移幂等，重复打开不重复写；旧 `legacy:` 隔离行仍按现有回填逻辑处理。

完成信号：迁移测试、认证测试、默认拒绝测试通过；planner 旧路径回归通过；没有产品行为变化。

### P4 授权图、派发与环路保护

新增 `send_to_bot`（或给 `submit_task` 增加 target 与 conversation 参数，二选一并写理由），由发起方 MCP 调用；协调器校验 sender 凭据与 `sender -> recipient` 边，创建 peer task，复用 `/v1/agent-runs` 派发。实现 `max_hops`、每对窗口上限、扇出上限、并发上限；超限返回类型化拒绝。重复 operation key 必须幂等。

完成信号：A→B 成功；A→C 无授权被拒；A→B→A 被环路或 hop 拒绝；重复 key 只产生一个任务；拒绝路径有审计事件。

### P5 回传、唤醒与崩溃恢复

结果只投递到发起方 inbox，使用发起方宿主铸造的 capability。发起方会话被唤醒，走现有 `pending_user_turn` 与 owner 语义。测试协调器在 dispatch 后、结果前崩溃；重复 delivery；发起方会话已关闭；接收方重启。所有路径收敛，不重复插入历史，不丢 receipt，不产生假终态。

完成信号：真实进程里 A 收到 B 的结果并继续一轮；强杀协调器后重启无重复、无丢失；发起方消失时保留待处理记录。

### P6 用量与缓存统计

实现第 5 节的读取器、接口与测试。读取器不导入 WebUI，不写 Bot 库。工作台之外先有 CLI 或 HTTP 可查，再接 UI。真实探针记录至少一个 Bot 的非零缓存读取；不报告的 Bot 显示 `null` 与 `coverage`。

完成信号：CACHE-01 至 CACHE-05 全绿；真实四 Bot 运行有每 Bot 数据。

### P7 工作台

在现有 WebUI 加 Bot 图、peer 消息列表、每 Bot 用量面板。空状态、加载失败、断线、窄屏、中英文都要覆盖。截图必须来自本次运行中的构建，不能用设计图。UI 不显示 raw capability、bearer、provider key 或消息正文。

完成信号：浏览器实际操作四 Bot 拓扑，截图与操作记录齐全；无敏感信息。

### P8 交付与独立审核

更新 README、DESIGN、CHANGELOG、`agent-plugin/.../SKILL.md`、示例配置。版本从 0.5.0 升到 0.6.0（协议新增，按 semver）。重建 wheel 与 sdist，在全新虚拟环境安装并跑 `nanobot-teams --help` 与最小流程。跑完整测试矩阵。用新上下文做独立对抗审核（Q1），修复发现（Q2），再做最终交付检查（Q3）。创建分支与 PR；PR 的 `## Why` 第一段写用户目标。独立审核通过后才合并；不 force-push。同步独立仓库并确认 Ubuntu/Windows CI。

完成信号：第 7 节验收矩阵全部有适用于最终代码的证据；双平台 CI 绿；wheel 可安装；文档与代码一致；审核发现闭环或有可核查的反证。

## 7. 验收矩阵

| ID | 断言 | 证据 |
|---|---|---|
| E2E-01 | 秘书长向 worker 派任务并收到结果 | 真实进程日志、task id、receipt |
| E2E-02 | worker A 向 worker B 派任务并收到回复 | 同上，含 sender/recipient |
| E2E-03 | B 的回复唤醒 A 的会话并继续一轮 | A 的会话历史、turn id |
| E2E-04 | A 汇总给秘书长，用户只看到秘书长 | 用户会话只有秘书长轮次；worker 的 peer 轮次不进用户会话 |
| E2E-05 | 无授权边被拒绝 | 类型化错误码、审计事件 |
| E2E-06 | 第五跳被 hop guard 拒绝 | 拒绝事件、hop 计数 |
| E2E-07 | 重复 operation key 幂等 | 单任务、单 receipt |
| E2E-08 | 协调器强杀重启不重复不丢单 | 强杀点、重启日志、最终状态 |
| E2E-09 | 发起方会话关闭后结果保留可查 | receipt、恢复扫描 |
| E2E-10 | 四 Bot 真实模型链路跑通 | 模型 ID、耗时、结果 |
| CACHE-01 | 60/100 与 50/NULL 的精确算术 | fixture 测试输出 |
| CACHE-02 | 缺失值不进分母、不显示 0% | API 响应、UI 截图 |
| CACHE-03 | 缺失库/未知 schema 返回 unavailable | API 响应 |
| CACHE-04 | 多模型拆分正确 | fixture 测试 |
| CACHE-05 | 时区日界正确 | fixture 测试 |
| SEC-01 | 伪造 sender 被拒 | 请求与响应 |
| SEC-02 | 凭据作用域隔离 | 越权请求被拒 |
| SEC-03 | 日志/UI 无 secret 与正文 | 扫描结果 |
| MIG-01 | 旧单 planner 数据库升级后行为不变 | 迁移前后对比 |
| UI-01 | Bot 图与用量面板可用 | 运行中截图 |
| UI-02 | 空/错/断线/窄屏状态 | 截图 |
| UI-03 | 中英文文案完整 | 截图、locale 检查 |
| OPS-01 | 新 venv 安装 wheel 并跑通 | 命令与输出 |
| OPS-02 | Ubuntu 与 Windows CI 绿 | CI 链接 |

同一测试可以覆盖多项，但不能只写“测试通过”。每项记录入口、命令、退出码、证据路径、适用代码指纹。

## 8. 运行环境

- 工作树：`C:\Users\KDB\orca\workspaces\nanobot\nanobot-teams-mvp`。不是相邻的 `nanobot-contribute`。
- Python：`C:/Users/KDB/orca/workspaces/nanobot/nanobot-contribute/.venv/Scripts/python.exe`。
- `PYTHONPATH`：工作树根 + `nanobot-teams/src`；Windows 用 `;` 分隔，不能用 `:`，否则子进程导入 `nanobot_teams` 失败。
- Teams 基线：`python -m pytest nanobot-teams/tests -q`，基线 281 passed。
- Nanobot 相关：选中套件 371 passed / 3 skipped；`tests/agent` 有 10 个既有 fake session 失败，属环境基线。
- 静态检查：`ruff check`；`basedpyright --pythonpath <venv>`。ruff 版本已固定，不要 `ruff format`。
- WebUI：`cd webui && bunx vitest run`、`bun run typecheck`。截图用运行中的构建。
- GitHub：主仓 `KDB-Wind/nanobot`；独立插件仓 `KDB-Wind/nanobot-teams`；同步用 `git subtree split -P nanobot-teams`。
- 真实模型：已配置的 Step Plan 与 GLM 可用且不限额度；记录实际 model ID，不按显示名猜。最终验收必须用真实模型。预算 2 亿以上 token：不为省 token 跳过真实测试，也不重复跑已通过的无关测试。

## 9. 不停止协议

- 只有第 7 节全部满足才能 `update_goal(complete)`。单个测试失败、阶段结束、进展慢、上下文压缩都不是停止理由。
- 阻塞时记录 `blocked`（现象、证据、已试方法、恢复步骤），转到下一个可推进单元；不空转、不 sleep 凑时长、不重复跑无关测试。
- 不可逆或共享状态动作（合并 main、force-push、发布 PyPI、删除数据、发外部消息）先完成全部前置；需要人工授权时写入 `nanobot-teams/OPERATOR_GATE.md`，继续其他工作，最后统一列出。
- 上下文压缩后先读 `PEER_COMMS_PROGRESS.md`、决策日志和 `git diff`，不重做已 verified 且代码未变的工作。
- 同一失败反复出现时，回到最早出错的边界，检查前提，换验证方法；不盲目重试，不降低断言。
- 独立审核用新上下文，先让审核者从契约找反例，再给实现解释；发现必须有代码位置、触发条件、影响、复现步骤。模型意见由代码或复现确认，不按自评分验收。
- 大批量只读审计、日志整理、跨文件搜索交给子代理；主线程保留决定、证据摘要和下一步，不把原始输出堆进上下文。
- 不允许 `reset --hard`、`git clean`、覆盖用户当前配置；保留未提交内容并先备份。

## 10. 交付物

- `nanobot-teams/PEER_COMMS_DESIGN.md`、`PEER_COMMS_BACKLOG.md`、`PEER_COMMS_PROGRESS.md`、`OPERATOR_GATE.md`（如需要）。
- 产品代码、迁移、测试、探针、fixture、UI。
- 更新后的 README、DESIGN、CHANGELOG、SKILL、示例配置。
- 0.6.0 wheel 与 sdist，新 venv 安装记录。
- 每个可验证单元一个提交；分支与 PR；独立审核记录。
- 最终报告：目标、完成条件、每项证据、限制、未做的 operator gate、启动与回滚入口。

## 11. 主提示词（/goal 用）

将下面整段作为一条消息发送。启动后把 `/goal` 后的正文完整保存为持久 objective。

```text
/goal 在 C:\Users\KDB\orca\workspaces\nanobot\nanobot-teams-mvp 完成 Nanobot Teams 的多 Bot 异步互通、每 Bot 缓存命中率统计，以及 0.5 工作台遗留交付项。你负责推进到可验证完成，不能只做计划、演示或总结。

全程使用 poteto-mode，由它选择并调用配套 skills。先按 Runtime Context 检查 active goal；没有就立即 create_goal，把本条正文完整写入 objective，确认成功后再开工。已有同一 active goal 则沿用，不重复创建。

先读根 AGENTS.md、.agent/*、nanobot-teams/ASYNC_WORKBENCH_ARCHITECTURE.md、ASYNC_WORKBENCH_IMPLEMENTATION_PLAN.md、REPAIR_STATUS.md、BLOCKERS.md、ROADMAP.md、PR16_REVIEW*.md，以及 D:\Code_Study\nanobot\TEAMS_PEER_COMMS_GOAL.md 全文。以该文档的契约、阶段和验收矩阵为准。保留当前未提交内容，先备份；禁止 reset --hard、clean、force-push、覆盖用户配置。

目标：任意两个已注册且已授权的 Bot 能异步派发任务并收到结果；每个 Bot 有独立身份、绑定和凭据；默认拒绝，显式授权；hop、环路、扇出有上限；结果只回到发起方；重启和重复投递收敛；用户只跟秘书长对话，worker 之间可互通。每个 Bot 的缓存命中率必须可查询、可在工作台看到，分母和缺失值按文档定义，不得用总数或 0% 代替。

按 P0–P8 推进，每个单元先红后绿，验证再前进：P0 基线审计与 backlog；P1 设计 ADR 与威胁模型；P2 四 Bot 失败探针；P3 身份与绑定迁移；P4 授权图与派发；P5 回传唤醒与崩溃恢复；P6 用量统计；P7 工作台 UI；P8 交付与独立审核。先证明“单 Bot 同时收发”需要哪些进程；若必须改宿主，写 ADR、默认关闭、向后兼容，并给出反证。不能靠 mock、内存队列、放宽断言或伪造证据通过。

真实模型可用，Step Plan 与 GLM 不限额；最终验收必须用真实进程和真实模型，假执行器只能用于控制崩溃点。保持 Windows 与 Ubuntu 双平台可跑；Windows PYTHONPATH 用分号。已知 tests/agent 的 10 个 MCP fake session 失败是环境基线，不要修。

维护 nanobot-teams/PEER_COMMS_PROGRESS.md，记录阶段、命令、退出码、证据、剩余项和下一动作；关键决定写 .audit/peer-comms-decisions.tsv。每约 30 分钟落盘，上下文压缩后先读它和 git diff，不重做旧工作。

只有满足 PEER_COMMS_GOAL.md 的全部完成条件（P0–P8、验收矩阵、双平台测试、类型检查、wheel 安装、独立审核、文档与 CI）才可 update_goal(complete)。单个测试失败、进展慢、阶段结束都不是停止理由；阶段汇报后直接执行下一项，不主动结束。阻塞就记录并继续其他可推进单元，不空转、不 sleep 凑时长。预算 2 亿以上 token，不为省 token 跳过真实测试。不可逆动作（合并 main、发布 PyPI、删除数据）先完成前置；需要人工授权时写入 OPERATOR_GATE.md 并继续，最后统一列出。不要用运行时长替代证据。
```
