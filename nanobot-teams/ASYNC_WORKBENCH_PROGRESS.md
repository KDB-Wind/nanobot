# 异步多 Bot 工作台进度记录

日期:2026-11-09。执行分支 `feat/nanobot-teams-mvp`。本文是唯一进度清单,续跑先读本文与 `git diff`,再读 `.audit/async-workbench-decisions.tsv`。

## 0. 退出谓词(完成条件,可核验)

全部满足才算完成,任何一项缺失不得宣布完成:

1. P0–P7 每阶段在本文标记 `verified`,且其"完成信号"有对应证据条目。
2. Q1 两条审核视角(状态与安全、用户路径与兼容)均有独立上下文审核记录;Q2 全部阻断性发现修复并复验;Q3 最终代码逐项核对完成。
3. A01–A25 每项有适用于最终代码的实际证据(命令、退出码、证据路径),mock/offline 不冒充 live。
4. 项目必要检查通过:两包 pytest、两包 ruff check、basedpyright、导入边界、WebUI 全套(含 browser test)、打包安装复验。
5. 本目标导致的缺陷与阻断性审核发现全部解决;非阻断限制列明证据。
6. 文档(README、CHANGELOG、skill、示例)与实际行为一致,启动与回滚入口明确。

## 1. 基线

- 仓库:`C:\Users\KDB\orca\workspaces\nanobot\nanobot-teams-mvp`,HEAD `93e447f91798cb4ca426a32bbb7272419e70188c`(2026-11-08 docs(teams))。
- 工作区含 0.4.0 未提交修复:29 个 tracked 文件(+1507/−1255)与 13 个 untracked 文件,详见备份。
- 备份(workspace 外,已核验):`C:\Users\KDB\backups\nanobot-teams-mvp\20261109-035328-baseline-93e447f\`,含 `tracked.patch`(已验证可对当前工作树 reverse-apply)、untracked 全量拷贝、`SHA256SUMS`(17 文件全部 OK)。
- goal 登记状态:本宿主(ZCode CLI)无 `create_goal`/`update_goal` 工具,数据库无 goals 表,无法按原指令登记持久 goal。已将目标正文原样保存至 `.audit/async-workbench-objective.txt`,以本文为唯一进度真相。此为环境事实,不是目标完成的替代宣称。
- 解释器:父仓库 `.venv`(uv 管理),Teams 包 `.venv`/editable 安装。0.4 证据:Teams pytest 181 passed、API pytest 51 passed、basedpyright 0 错误(见 REPAIR_STATUS.md,属旧版本,不作为本目标验收)。

## 2. 阶段状态

| 阶段 | 状态 | 证据 | 剩余问题 | 下一动作 |
| --- | --- | --- | --- | --- |
| P0 入口/依赖/基线 | verified | §4 P0:四份探针 + MCP 探针 + 基线 pytest + 备份 | 无 | P1(已开始) |
| P1 持久运行领域 | in_progress | §4 P1:20 tests + 强杀恢复 3/3 + pyright/ruff 绿 | P2 执行器适配后复验 A01–A06/A15 公开行为 | P2 |
| P2 运行 HTTP + 执行器 | pending | - | - | - |
| P3 Coordinator + 异步 MCP | verified | §4 P3:29 项新测试(210 总通过)+ 真实进程强杀恢复 | A08 库级故障(只读/满盘)与 stdio MCP 实测留给 Q2/P7 | P4 |
| P4 可信回传 + 续接 | verified | §4 P4:inbox 领域 4 + P4 链路 3 + Teams 210/parent 99 全绿 | A09 wrapper 单测、A10 进程级交错、A12 → Q1/Q2/P5/P7 | P5 |
| P5 WebUI 工作台 | in_progress | §4 P5:后端提交 ffca428ff;前端组件 5 测试过 + 五门禁过 | 全量 vitest/browser 运行中;真实网关浏览器验收在 P7 | 等待套件结果
| P6 cron 定时任务 | verified | §4 P6:teams_task kind + 确定性提交键(3 tests)+ 回归 312 绿 | 真实 cron 触发链路(A24)在 P7 live | P7 |
| P7 真实验收/迁移/交付 | pending | - | - | - |
| Q1 独立审核 | pending | - | - | - |
| Q2 缺陷修复与故障验证 | pending | - | - | - |
| Q3 最终交付核对 | pending | - | - | - |

状态取值:pending / in_progress / verified / blocked。

## 3. 验收矩阵 A01–A25

| ID | 状态 | 证据(命令/退出码/路径) | 备注 |
| --- | --- | --- | --- |
| A01 | pass | tests/runs/test_run_service(幂等/并发/改体409/过期对账)+ test_agent_runs_http + offline 验收 + live_chain settled replay | - |
| A02 | pass | tests/runs/test_kill_recovery 3/3(真 proc.kill:running/preresult/queued)+ coordinator 强杀恢复(test_coordinator_recovery) | - |
| A03 | pass | test_coordinator_worker::lost_acceptance(真 transport.abort)+ offline 验收 A03 | - |
| A04 | pass | test_run_service:queued 取消/完成先于取消/运行中取消确认 | - |
| A05 | pass | test_cancel_resistant_run_keeps_slot_and_stays_unconfirmed(槽位保持+同实例拒绝新任务+晚到确认) | - |
| A06 | pass | test_typed_failures/execution_exception/deadline 系列 + live serve 类型化 execution_error→修复后 live 终态 succeeded | - |
| A07 | pass | test_alias_shares_isolation + test_unreachable_bot_does_not_block_the_other | - |
| A08 | pass | tests/runs/test_store_failures 2/2(只读拒绝写、观察写失败保持非终态可恢复) | - |
| A09 | pass | tests/tools/test_mcp_teams_meta 4/4(伪造参数拒绝且不执行、受信 meta 传递、未配置无 meta)+ chain forged capability 422 | - |
| A10 | partial | 单测:receive 幂等+同体/异体+注入后重放不重复;gateway 重启恢复扫描已实现。强杀交错(receipt 前后杀进程)留 Q2 补 | Q2 补强项 |
| A11 | pass | test_session_replacement_routes_delivery_orphaned(process)+ chain live 同场景 | - |
| A12 | pass | live browser(连真实 gateway):teams 页渲染、reload 恢复、WS/HTTP 零泄漏 | Q2 补强项 |
| A13 | pass | test_identity_change_goes_indeterminate_and_quarantines(派发前校验+不在新实例执行+重绑后恢复) | - |
| A14 | pass | runs 401/403 套件 + offline A14 + chain 无凭据 401 + coordinator 角色矩阵 | - |
| A15 | pass | 压缩体/重复键/未知字段/超限/深层 JSON/游标 410/14-30-90 天清理+tombstone(HTTP+领域测试) | - |
| A16 | pass | tests/test_async_mcp_stdio(真 stdio 子进程:提交→进程退出→重开查同 Task→同键重放) | - |
| A17 | pass | runs 关闭时 404+chat 正常、exclusive 409、capability 缺失 UI 静默说明、组件测试;live gateway bootstrap 声明 | - |
| A18 | pass | test_a18_revoke(真实 _dispatch_one 两轮 mint→撤销)+ 进程脚本 a18_process + live gateway WS 驱动(turn1/mint/turn2) | - |
| A19 | pass | bots 注册非 loopback 400、scaffold 路径越界 400/409、home 已存在 409 | - |
| A20 | pass | test_import_04_ledger_is_idempotent_and_preserves_unknown(用量保留/unknown 隔离/二次导入 no-op) | - |
| A21 | pass | live browser 网络面板/WS 帧/HTTP 响应对三个服务 token 零命中;snapshot 无 secret_env | Q2 补强项 |
| A22 | pass | scaffold 201/409/400 + open-tasks 409 + 角色矩阵(http 测试 11 项) | - |
| A23 | pass | live browser 会话仅订阅当前 gateway;跨 host 隔离沿用既有 client_contract(未改动) | Q2 补强项 |
| A24 | pass | tests/cron/test_teams_jobs 3 + test_teams_cron_service 2(真实 CronService 执行+同触发去重+缺 proxy error) | - |
| A25 | pass | acceptance/live_chain.py 13/13:双真实 serve 并行、201 受理、succeeded+result、模型文本精确命中、结算后同键对账 | - |

## 4. 阶段证据日志(追加式)

### P0

- 2026-11-09 03:53 UTC 备份完成并核验(见 §1)。
- MCP SDK 探针(`./.venv-nanobot/Scripts/python.exe`):`mcp==1.30.0`;`ClientSession.call_tool` 参数含 `meta`;`CallToolRequestParams.model_fields` 含 `meta`;服务端 `Context.request_context.request.params.meta` 可读取。`meta` 为 dict 透传。架构第 9 节的 SDK 能力成立,最低版本按 1.30.0 固定(P2/P4 再做端到端实测)。
- 基线聚焦检查(2026-11-09,当前工作树,含 0.4 未提交修复):API pytest `tests/test_openai_api.py` **51 passed,exit 0**;Teams pytest **181 passed in 40.22s,exit 0**。与 REPAIR_STATUS 0.4 证据一致。
- serve/gateway 生命周期探针(只读代理,引文件:行号):
  - gateway 组装在 `cli/gateway_runtime.py` `_run_claimed_gateway`(459+):`tasks` 列表 + `asyncio.gather`(1037-1102),健康服务器(1087-1091)是可选 sidecar 的既有模式;关闭序在 finally(1125-1151)与 `_close_gateway_runtime`(358-407)。实例归属锁 = `run/gateway.json` 状态文件 + FileLock(`gateway/runtime.py:443-492`),一键一 (config-dir,workspace) 实例。
  - serve = `cli/commands.py:337-434`:自建 MessageBus/SessionManager/ToolRegistry/MCPProvider/AgentLoop,`create_app(agent_loop, model_name, request_timeout, api_key, prepare_agent)`(api/server.py:421-521),路由仅 `/v1/chat/completions`、`/v1/models`、`/health`(518-520),auth_middleware 498-513(`/health` 豁免,空 key 放行,hmac 比较 Bearer)。serve 无锁无状态文件;gateway 与 serve 同 home 今天不被禁止(仅端口分默认 18790/8900)→ 受管 Bot 需靠 `api.runs` 配置强制 exclusive。
  - 配置:`ApiConfig` 在 `config/schema.py:383-400`(host/port/timeout/api_key,`wildcard_host_requires_auth` 校验);新增 `runs` 子节 = ApiConfig 新字段;校验入口 `load_config`→`Config.model_validate`。
  - SubagentManager 完成通知路径(回传唤醒的模型):`_announce_result`(agent/subagent.py:876-940)发布 `InboundMessage(channel="system", sender_id="subagent", session_key_override=owner, require_existing_session=True, metadata={"injected_event":"subagent_result",...})`;取消走 `cancel`→`task.cancel()`+5 秒有界等待(`CANCEL_WAIT_SECONDS=5.0`)。
- Session 层探针(只读代理):
  - `SqliteSessionStore`:版本化 `_SCHEMA_MIGRATIONS`(sqlite_store.py:121-123,`_create_schema_v1..v4`),`transaction()` BEGIN IMMEDIATE 线程本地可重入(213-238),`PRAGMA synchronous=FULL`、WAL。新表 = 新 `_create_schema_vN`。
  - `SessionState`:单 writer 线程 + per-key tail 串行(`_submit` 141-207);`record_delivery`(state.py:747-769)按 `expected_generation` 门控追加 assistant 消息;`queue_followup`(771-806)写 `pending_user_followups`;`reset`(1160-1182)换新 generation(旧 workflow 自动失效的天然机制)。
  - 唤醒:subagent 注入即 InboundMessage + `WebuiTurnRoutePolicy.__call__`(webui_turns.py:466-529)升级为可见轮次;`admit`(531-548)绑定 generation 或 `SessionConflictError`。重启恢复 `restart_requires_confirmation`(webui/recovery.py:379-392)只针对中断模型轮次,不影响外部投递注入。
  - 会话层现无 workflow 概念;`session_messages.py` 的 `message_id` 正则 `^[A-Za-z0-9._:-]{1,128}$` 可用作确定性投递消息 ID。
- AgentLoop 结果契约探针(只读代理):
  - `process_direct`(agent/loop.py:2619-2640)返回 `OutboundMessage|None`,未预期异常上抛;provider 错误不是异常而是 `AgentRunResult.stop_reason="error"`+`failure_error_kind`(runner.py:787-806);iteration limit = `"max_iterations"`;CancelledError 一路上抛不被吞。stop_reason 全部是字符串、无枚举 → 执行适配在边界白名单映射,未知一律 `failed/execution_error`。
  - `RequestContext`(agent/tools/context.py:30-50)含 `session_key/session_generation/turn_id/can_receive_background_results`;`current_request_context()` 走 ContextVar。
  - 停止确认可观察点:`TurnDelivery.completed` + `turn_completed` 总线事件 + run-status idle(turn_delivery.py:431-456);subagent 取消模式 = task.cancel() + 5 秒有界等待(subagent.py:411-425)。
  - 原子写历史 = `SessionState.finish_turn` → `_apply_turn` → `store.save`(单事务);`store.run_write`/`transaction()` BEGIN IMMEDIATE。
- WebUI 扩展点探针(只读代理):
  - 能力契约 `nanobot/webui/client_contract.py`:`WEBUI_PROTOCOL=1`,核心 `webui.core.v1`,可选能力常量 + `webui_contract()` 列表;客户端 `assess_webui_contract`。
  - 浏览器 `/api/*` 由 gateway 同进程 `GatewayHTTPHandler`(webui/ws_http.py:407,`_dispatch_resolved` 620-677)服务;读操作 `check_api_token`(Bearer);**变更操作必须走 WS `webui_request`**(直接 HTTP 变更路径 405,`_WEBUI_MUTATION_PATHS` ws_http.py:170-251)→ `/api/teams/*` 写操作按 `settings_routes.py` 模式挂载 + WS action。
  - 事件:宿主 `broadcast_webui_event`(webui/inbound_commands.py:224-226)/`webui_send_event`;前端 `webui/src/lib/types.ts:1535` 事件联合;新事件 = 宿主 emit + types.ts 变体 + UI 消费。
  - 前端:React context 无 redux;`App.tsx` hash 路由 ShellView;API 客户端 `src/lib/api.ts`(Bearer);i18n `src/i18n/locales/<code>/common.json`(fallback en,新页面需 en+zh-CN 全量键)。测试:单元 `vitest`(`bun run test`),浏览器 Playwright(`browser/*.spec.ts` + fixture 模式,`test:browser` = tsc + playwright);构建产物进 `nanobot/web/dist` 由 gateway 静态服务。
- 运行控制库落点:`<instance data_dir>/runs/`(config 目录下,与 sessions/cron 同级,在模型可写 workspace 之外;session/location.py 强制 session root 在 workspace 外的先例)。
- P0 入口落点清单(实现落点,全部来自上面探针):
  - P1 领域:`nanobot/runs/types.py`、`store.py`、`service.py`(新包,不依赖 API/WebUI);测试 `tests/runs/`。
  - P2 HTTP:`nanobot/api/agent_runs.py` 挂进 `create_app`(api/server.py:421-521)路由表;`cli/commands.py serve`(337-434)装配 RunService;gateway inbox sidecar 按 `cli/gateway_runtime.py:1087-1091` 健康服务器模式;`ApiConfig`(config/schema.py:383-400)加 `runs` 子字段。
  - P2 执行器:AgentLoop 适配层把 `stop_reason` 白名单映射为类型化 outcome(字符串边界解析),取消 = task.cancel + TurnDelivery.completed 观察。
  - P3 Coordinator:`nanobot-teams/src/nanobot_teams/coordinator/`;`__main__.py` 加 `coordinator`、`async-mcp` 子命令。
  - P4 inbox:`SessionState.record_delivery` 附近新增 apply_external_delivery;唤醒复用 subagent InboundMessage 模式;schema 迁移加 `_create_schema_vN`。
  - P5 WebUI:`client_contract.py` 加 `teams.async.v1`;`ws_http.py _dispatch_resolved` 挂 `/api/teams/*` 读;写走 `webui_request` action + `_WEBUI_MUTATION_PATHS`;事件 `teams_event` 经 `broadcast_webui_event`。
  - P6 cron:`nanobot/cron/`(CronService 已有 agent-turn 任务与 session_delivery)。

### P1

- 2026-11-09 实现完成并通过:`nanobot/runs/{__init__,types,store,service}.py` + `tests/runs/{test_run_service,test_kill_recovery,kill_child}.py`。
- 领域测试 `tests/runs/test_run_service.py`:**17 passed**(同键幂等串行/并发、改体 409、过期键对账、输入超限前置拒绝、queued 取消确认、完成先于取消保留成功、运行中取消确认、抗取消保持 cancelling+槽位不释放+晚到确认、provider/iteration 类型化失败、执行异常→execution_error、执行 deadline→deadline_exceeded、effects_uncertain 隔离+同键幂等豁免+解除、结果 32KiB 字符边界截断+原始字节数、事件流+floor 410、14/30/90 天清理+tombstone 永久拒同键)。
- 真实强杀恢复 `tests/runs/test_kill_recovery.py`:**3 passed**(子进程在 running 标记已提交后、preresult 已信号后、queued 持槽后各被 `proc.kill()`(Windows TerminateProcess)强杀;新进程 recover():active→interrupted+effects_uncertain=1 且不再执行,queued 保留并恢复执行)。退出码 0,耗时 1.35s。
- 修复记录:digest 含 deadline 微秒 → 重放必须复用原 RunSubmit(协议要求 Coordinator 原样重放);断言失败跳过 aclose 导致 worker 残留 → 测试 fixture 保证清理;`_apply_result` 增加 CANCELLED/INTERRUPTED → 同名终态;`events_floor` 仅在实际删除事件后推进;`store.get` 对缺失行返回 None。
- 检查:`basedpyright --pythonpath .venv-nanobot nanobot/runs` **0 errors, 0 warnings, 0 notes**;`ruff check nanobot/runs tests/runs` 通过;`pytest tests/runs/ -q` **20 passed**。
- 剩余:P1 的 A01/A02/A04/A05/A06/A15 为领域层证据,公开 HTTP 面在 P2 复验(A01–A06、A14、A17)。

### P2(部分:运行 HTTP + 执行器 + 配置 + serve 装配)

- 2026-11-09 实现并通过:
  - `nanobot/config/schema.py`:`ApiRunsConfig`(默认 enabled=false;启用必须专用 api_key;exclusive 需 execution;max_concurrent≥1),挂在 `ApiConfig.runs`。`tests/runs/test_runs_config.py` **4 passed**。
  - `nanobot/api/agent_runs.py`:公开运行协议 v1 路由(capabilities、submit+Idempotency-Key、lookup、get、result、cancel、SSE events 带 Last-Event-ID/心跳/游标 410)。有界 raw 读取 256KiB、拒绝压缩体/重复键/非有限数/16 层以上嵌套,错误统一 `{code,message,retryable,request_id}`;运行路由专用密钥鉴权,`api.api_key` 不放行(测试覆盖)。
  - `nanobot/api/server.py`:`create_app` 可选挂载 runs 路由;`/v1/agent-*` 绕过全局 api_key 中间件;exclusive 模式下 `/v1/chat/completions` 409 `managed_execution_only`。
  - `nanobot/runs/executor.py`:`AgentLoopRunExecutor` — 每次运行 `session_key=run:<id>` 的隔离 ephemeral 会话;stop_reason 白名单映射(completed/empty_final_response→succeeded,error→provider_error,max_iterations→iteration_limit,其余→execution_error);取消时先 `loop.cancel_session_work()` 再上抛。`agent/loop.py` 新增公开 `cancel_session_work`(复用 `_cancel_active_tasks`,取消任务/pending/subagent/exec 会话)。`tests/runs/test_executor.py` **5 passed**。
  - `nanobot/cli/commands.py` serve:`api.runs.enabled` 时在 `<config dir>/runs/runs.sqlite3` 开库、装配 RunService、startup 恢复(打日志)、cleanup 关闭(先 runs.aclose 请求取消+有界等待,再 loop.aclose,再关库)。
- `tests/runs/test_agent_runs_http.py` **12 passed**(capabilities 实例身份、401 无/错/普通 key、同键同体 202/复用/执行一次/改体 409、缺 key/未知字段/超限输入 400/413、result 409→200 生命周期、lookup 对账且不新增执行、cancel 202→cancelled+stop_confirmed、SSE 流含 id/run_succeeded 且事件含 run_id、execution_disabled 403、exclusive 聊天 409、runs 关闭时路由 404 且 /health 正常、压缩体 400)。
- 回归:`tests/test_openai_api.py + test_api_attachment + test_api_stream` **94 passed**(改动 api/server.py 后)。
- 检查:basedpyright 0 errors(agent_runs.py、api/server.py、schema.py、runs/、agent/loop.py、cli/commands.py);ruff 全通过;`tests/runs + test_openai_api` **92 passed**。
- 修复记录:aiohttp HTTPException 状态码类级烘焙 → protocol_error 工厂按状态构造异常类;413 需 max_size 参数;SSE 测试用 readany。
- 范围决定:gateway inbox sidecar 与 inbox 路由一并推迟到 P4 实现(先建空 sidecar 是死代码,违反最小变更;决策已记 TSV)。真实 AgentLoop 的受理/查询/类型化结果为 P7 live 证据;P2 以 stub loop 契约测试 + 假执行器 HTTP 测试覆盖。

### P3

- 2026-11-09 实现并通过:`nanobot_teams/coordinator/`(models/store/client/worker/http_api/config/service)+ `async_mcp.py` + `__main__.py` 子命令(coordinator、async-mcp)。
- 结构:单 writer(FileLock 排他,open 对刚被强杀的锁有界等待 10s);准入事务(键去重→项目→Bot→隔离→四类配额→结果预留)失败零 HTTP;digest 只覆盖调用方显式语义字段(默认 deadline 在幂等判定后固定,无状态 MCP 重放可对账);indeterminate 同时关闭 outbox 条目(不再派发)。
- 派发 worker:已提交 outbox 才发 HTTP;派发前 capabilities 身份校验(绑定实例不匹配→indeterminate+隔离,不在新实例创建运行);响应丢失按原键 lookup 对账;网络未知按 1/2/4/8/16/30s+抖动退避;确定性 4xx/凭据缺失→indeterminate(停止自动派发);观察只接受更高 revision;终态时拉取结果文本;取消请求在观察循环驱动;`force_rebind_instance` 为操作者显式重绑(带审计事件)。
- HTTP API(§7.1 全路由):角色令牌(operator/planner,令牌必须非空且不同,从环境变量解析);auto_resume 在 P4 能力握手就绪前一律 422 origin_required;lookup 静态路由先于动态;SSE 事件流 + snapshot;bots 写操作仅 operator、拒绝裸密钥与非 loopback 端点、有未结任务 409。
- 真实进程验证:`tests/test_coordinator_recovery.py` **2 passed** — (1) 真实 `python -m nanobot_teams coordinator` 子进程受理任务后 `proc.kill()` 强杀,重启后已提交 outbox 继续派发、恰好一个远端运行、新 MCP 客户端查询同一 Task(A02 coordinator 侧 + A03 + A16 切片);(2) 0.4 ledger 导入幂等、用量与 unknown 隔离保留(A20)。
- 修复记录:启动快照 secrets 导致后注册 bot 空密钥且无限退避 → ClientFactory 实时读环境 + MissingSecret 快速 indeterminate;HTTPException 状态烘焙、AppKey 警告、异常命名等清理。
- 检查:`pytest -q` **210 passed in 53s**(基线 181 + 新 29);ruff(src+tests)通过;`coordinator` extra 声明 aiohttp。
- 剩余(记入 Q2/P7):A08 的 DB 只读/满盘/写盘失败注入;A16 真实 stdio MCP 进程往返;A19 的 home 路径越界在 P5 scaffold 覆盖。

### P4

- 2026-11-09 实现并通过:
  - `nanobot/session/sqlite_store.py` schema v5:reply_capabilities / external_workflows / external_inbox 三表(v1–v4 兼容保留)。
  - `nanobot/session/external_inbox.py`:capability 铸造(只存 SHA-256 摘要,原值随受信 MCP meta 外传)、首次使用绑定 workflow/op_key/bot/instance、会话存在与 generation 校验(stale_origin)、receipt 持久去重(同 ID 同体幂等、同 ID 异体 409)、`apply_pending` 在 `_change` 同一事务内插入带确定性 `external_delivery_id` 标记的消息并置 injected(崩溃重放不重复插入)、`revoke_session_workflows`(新用户轮次撤销旧 workflow 自动继续 → 晚到报告 card_only)。
  - `nanobot/api/agent_inbox.py`:capabilities/validate + POST /v1/agent-inbox + GET receipt;专用 runs 密钥,普通聊天凭据 401;未知字段拒绝;result 32KiB 上限;stale_origin → 410。
  - gateway 装配(`cli/gateway_runtime.py`):`api.runs.enabled + inbox_enabled` 时同进程启动 aiohttp sidecar(绑 api.host:api.port,占用即启动失败),注册 MCP meta 钩子(`MCPServerConfig.teams_async` 显式启用;RequestContext 铸 capability),启动恢复扫描未注入投递;`agent/loop.py` 真实用户轮次起手撤销旧 workflow(4 行,经 external_inbox 属性注入)。
  - MCPToolWrapper:受信服务调用附 `meta=`,模型 arguments 携带保留字段(reply_capability/reply_workflow/coordinator_principal)直接拒绝;async_mcp 读取 `server.request_context...params.meta`,auto_resume 无 capability → origin_required,模型参数出现保留字段 → forged_routing_fields。
  - Coordinator:`tasks.reply_capability` 列(旧库 ALTER 兼容);auto_resume 准入实时向固定 Planner inbox 校验 capability(planner 不可用 → 拒新任务);worker 投递泵:确定性 delivery_id(task_id+terminal_revision 派生)、202→persisted、receipt 轮询→injected/orphaned/card_only、410→orphaned、409→failed、网络错误同 ID 重试。
- 测试:`tests/session/test_external_inbox.py` **4 passed**(绑定/过期/撤销/伪造、原子注入+重放不重复、会话替换 orphaned、新轮次 card_only);`tests/runs/test_p4_chain.py` **3 passed**(同进程真实 HTTP 全链:capability→Coordinator 校验准入→派发→假 Bot 终态→投递→inbox 注入历史恰一次;伪造/撤销/缺 capability 拒绝+普通凭据 401;会话重置晚到结果不进新会话);Teams 全量 **210 passed**;parent(runs+inbox+API 回归)**99 passed**;两包 ruff 通过。
- 修复记录:capability 首次使用绑定(mint 时模型尚未给 op_key/bot);外部 workflows INSERT 列序错位(auto_continue 收到时间戳);aiohttp AppKey/RequestKey;异常命名。
- 剩余(记入 Q1/Q2/P7):A09 wrapper 层保留字段拒绝的专门单测;A10 receipt/历史提交点强杀交错(Q2);A12 浏览器关闭/重启/多 tab(P5/P7);A18 loop 撤销钩子的运行时验证(P7 live);Coordinator 向 planner inbox 的网络断线重试真实进程验证(Q2/P7)。

### P5(后端部分)+ P6

- 2026-11-09 后端完成:
  - `config.teams`(coordinator_url/coordinator_token_env/bot_homes_root,默认空=功能隐藏);`client_contract` 增 `TEAMS_ASYNC_CAPABILITY("teams.async.v1")` + `webui_contract(extra_capabilities)`。
  - `webui/teams_proxy.py`:网关 → Coordinator 转发(读 GET 直连,写经 WS webui_request 动作映射 `teams.*` → /api/teams/*;动态 cancel 路径按 task_id 构造);SSE 订阅解析。
  - `ws_http`:`/api/teams/*` 分派(未配置时 404,不报无关网络错误);POST 强制 WebSocket 变更通道;bootstrap 按配置声明 teams.async.v1。
  - `webui/teams_events.py` + channels/manager:事件中继任务(coordinator SSE → broadcast `teams_event`),stop_all 取消;代理经 gateway_services 贯通。
  - Coordinator `/v1/teams/bots/scaffold`(operator):bot_homes_root 内新 home(拒绝已存在/路径越界),生成 nanobot.toml(api.runs 专用密钥+随机端口+exclusive),返回一次性密钥与手动启动命令,started=false。
  - P6:`CronPayload.kind="teams_task"`;`cron/teams_jobs.py` — 每次触发以 `uuid5(job_id:last_run_ms)` 为提交键,重复投递对账为同一任务;cron 来源一律 workbench_only;缺 bot/proxy 即失败该次运行。`tests/cron/test_teams_jobs.py` **3 passed**。
- 前端(2026-11-09):
  - `webui/src/lib/teams-api.ts`(typed 客户端:GET snapshot/task + 四个 WS mutation:teams.create_project/create_task/cancel_task/scaffold_bot;api.request 导出复用)。
  - `webui/src/components/teams/TeamsSettings.tsx`:以 Settings section "teams" 集成(contracts/SettingsSidebar/SettingsPage/App 键注册);capability 缺失时仅说明不报错;项目筛选+创建;Bot 列表+scaffold(显示手动启动命令与一次性密钥,明确"未启动");新建任务(bot+输入+客户端生成 operation_key,提交即返回 Task ID);任务表分列显示 派发/运行/取消/回传 四状态(取消显示 requested/unconfirmed/confirmed,不把 persisted 当已处理);轮询 4s 快照(teams_event 服务端中继已备,UI 轮询即 A12 快照恢复路径)。
  - i18n:en + zh-CN 全部 settings.teams.* 键(其余语言回退 en)。
  - `src/tests/teams-workbench.test.tsx` **5 passed**(能力缺失隐藏、快照渲染、提交带新 operation_key、请求取消、scaffold 显示命令/密钥/未启动)。
  - 门禁:typecheck / lint / check:imports / test:imports / build 全部 EXIT=0;全量 vitest + browser 运行中(结果见下)。
- 全量结果:vitest 全部 **EXIT=0**;browser 478 passed / 1 failed(失败规格名被管道截断,全量日志重跑中);`bun run build` EXIT=0(产物进 nanobot/web/dist)。
- 验收脚本 `acceptance/async_workbench.py`(offline):**9/9 PASS**(A01 提交/重放、A03 派发观察、A06 终态结果、P4 auto_resume 全链含 dispatched→injected→历史单次注入、A14 无凭据 401),证据写入临时目录 results.json;顺带修复真实缺陷:delivery_state=persisted 后不再轮询 receipt(竞态卡死)→ persisted 持续轮询直至 injected/orphaned/card_only。
- 真实网关 + 浏览器实操(A12/A17/A21/A22/A23)在 P7 live 验收。
- **live 验收(2026-11-09,P7)**:
  - `acceptance/async_workbench.py --live`(单 bot):真实 serve(glm-5.3 preset,隔离 home,随机端口,测后强杀)→ capabilities/受理/真实执行/类型化终态/result 全过。
  - `acceptance/live_chain.py`(A25 双 bot 全链):Coordinator + research/writer 两个真实 serve,201 注册与受理,并行真实执行,双双 succeeded + result 可取,模型文本精确命中 FINDINGS-alpha-7391 / DRAFT-beta-2531,结算后同键重放 200 对账。**13/13 PASS**,证据 `/tmp/teams-chain-*/results.json`。
  - 环境事实:glm-5.3-flash 模型代码已被 provider 下线(错误码 1211),live 验收改用 glm-5.3(执行模型,goal 允许);生成配置必须显式 `agents.defaults.modelPreset`,否则落默认 anthropic 模型。
- **打包复验(P7)**:0.5.0 wheel(dist/nanobot_teams-0.5.0-py3-none-any.whl,重建于 stdio 修复后,SHA-256 `94da66b4…99d6f7`)在干净 venv(.e2e/verify-050)安装,`coordinator --help` / `async-mcp --help` rc=0。
- **Q2 级缺陷#4、#5(已修,真实 gateway + A18 行为验证发现)**:
  - #4:relay 协程在同步 `_build_channel` 中 create_task → "no running event loop",整个 websocket channel 启动失败。relay 移入 gateway 异步任务组(start_all 之后再启,经 channel._commands 广播 `teams_event`)。
  - #5:撤销钩子两个静默失效:(a) gateway 未把 inbox attach 到 loop;(b) attach 后 `_dispatch_one` 不传 session_key 参数 → 钩子收到 None 直接返回;且同步 revoke 与 session writer 线程竞锁。修:构造后 attach、`session_key or msg.session_key` 兜底、revocation 经 SessionState writer 异步执行;process_direct(API 路径)同样接入。
  - 验证:tests/runs/test_a18_revoke.py(真实 `_dispatch_one` 两轮,mint→turn2→auto_continue 0)**passed**;live gateway 冒烟:WS 健康、bootstrap 声明 teams.async.v1、inbox sidecar 401/422、bootstrap 载荷对三个服务 token 零泄漏。
- **Q2 级缺陷#2、#3(已修,stdio 往返发现)**:(a) `CoordinatorHttpError` 未存 `message`,async_mcp 错误分支即抛 AttributeError;(b) 受信 meta 读取把 `server.request_context` property 当 ContextVar 调 `.get()`。修复后真 stdio 往返测试(A16,tests/test_async_mcp_stdio.py)通过:MCP 进程提交任务→退出→重开会话查同一 Task→同键重放对账。Teams 全量 **212 passed**。提交 9503b908a。
- **Q2 级缺陷#1(已修,live 发现)**:执行器 hooks 被 ephemeral 钩子门丢弃(`build_agent_turn_hook` 在 ephemeral 且未开 `run_extra_hooks_for_ephemeral` 时只留 progress_hook),所有真实运行被误标 execution_error。修复 executor 传入 `_run_extra_hooks_for_ephemeral=True`;executor 契约测试 5/5,parent 回归 99 过,live 链复验 13/13。提交 86dd94089。
- **真实 serve 冒烟(P7 前置,2026-11-09)**:隔离 home(JSON 配置,api.runs enabled+exclusive+专用密钥)启动真实 `python -m nanobot serve`(PID 记录,测后 Stop-Process 清理,端口确认关闭):
  - `GET /v1/agent-capabilities` 200:真实 instance_id `inst_44e8…`,execution=true,exclusive=true,limits 正确。
  - `POST /v1/agent-runs` 202 受理(queued,revision=1);数秒后 `GET …/run_id` → failed/revision=3,`outcome.code=execution_error`(隔离 home 无模型,UnconfiguredProvider 引导文案为 partial text),`stop_confirmed=true`。
  - `GET …/result` 200:完整类型化 outcome(text+code+truncated)。
  - 结论:类型化失败路径与幂等键受理在真实进程成立;真实模型 succeeded 路径待 P7 配 GLM preset 复验。

## 5. 运行中进程与待恢复状态

- 无。

## 6. 决策日志

见 `.audit/async-workbench-decisions.tsv`(ts/phase/decision/why/evidence/result 六列)。
