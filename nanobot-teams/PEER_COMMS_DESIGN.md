# Peer Comms 设计（ADR 与威胁模型）

P1 交付物。契约来源：[PEER_COMMS_GOAL.md](PEER_COMMS_GOAL.md) §4 领域契约（I1–I10）、§5 缓存命中率契约、§6 阶段。行号证据见 [PEER_COMMS_BACKLOG.md](PEER_COMMS_BACKLOG.md)（2026-11-10 勘察）。

## 1. 运行时问题：单 Bot 同时收发需要什么

**问题**（目标 §3）：一个 Bot 同时接收 inbox 投递与执行 runs，是单进程、双进程，还是需要宿主改动。

**探针**（`.audit/serve_inbox_probe.py`，真实 `nanobot serve` 子进程，2026-11-10 运行）：

```json
{"probe": "serve_inbox", "capabilities_status": 200,
 "capabilities_inbox_flag": true, "inbox_post_status": 404,
 "route_exists": false, "verdict": "RED"}
```

事实：`serve` 配置 `api.runs.inboxEnabled=true` 后，`GET /v1/agent-capabilities` 照配置**宣告** `inbox: true`（`nanobot/api/agent_runs.py:196` 直读 config），但 `POST /v1/agent-inbox` 返回 404——inbox 路由只挂在 gateway 的 sidecar（`nanobot/cli/gateway_runtime.py:630-670`），`serve` 装配路径零处读取 inbox（`cli/commands.py` 无 inbox 引用）。宣告与路由不符，即当前代码在撒谎。

**结论：需要一处宿主改动（ADR-001），不是双进程。** 双进程方案（每 worker 跑 serve + 另一个带 inbox 的进程共享 home）被架构文档 §3 明确禁止：会话库两个写入者，唤醒也无法到达活着的 AgentLoop。把 RunService 加进 gateway 是更大的改动（gateway 无 runs 组装，serve 已有全套）。

### ADR-001：serve 增加可选 runs inbox

- **变更面**：`serve` 装配处（`cli/commands.py` → `api/server.py`），当 `api.runs.enabled and api.runs.inbox_enabled` 时，在 serve 的同一 aiohttp app 上：构造 `ExternalInbox(session_manager.state)`、`register_inbox_routes(app, inbox, runs_cfg)`（与 runs 路由同 app 同端口，无 sidecar 端口冲突）、`set_teams_async_meta_hook`（capability 铸造，`gateway_runtime.py:153-182` 同款）、接入 `InboxWakePump`（`session/inbox_wake.py`）到 loop 生命周期。
- **默认关闭**：`inbox_enabled` 现默认 False（`config/schema.py:395`），不变。
- **向后兼容**：不配置 inbox 的部署装配路径与今天逐字节等价；capabilities 语义从"照抄配置"变为"如实宣告"。
- **反证条件**（目标 P1 要求）：若 serve 的会话生命周期无法承载 wake pump 而不破坏 exclusiveExecution 契约（probe/acceptance 出现 wake 丢失或双写会话库），回退双进程方案并把回退证据写回本文档。探针 `.audit/serve_inbox_probe.py` 在 P3 落地后必须翻绿。

## 2. 领域对象

| 对象 | 载体 | 关键字段与不变量 |
| --- | --- | --- |
| `Principal` | 协调器令牌映射（启动时构建） | 身份来自令牌验证，非自报。三类：`operator`（admin scope）、`planner`（submit + 收件）、`bot:<bot_id>`（submit + receive，scope 至少区分 submit/receive/admin，bot 永不 admin）。令牌只存环境引用名（`bots.coord_secret_env`），值不落库不进日志（I1） |
| `BotBinding` | `bots` 表加列（不建平行注册表） | 现有列：`bot_id, display_name, role, endpoint, secret_env, enabled, instance_id, capabilities`（`coordinator/store.py:43-55`）。新增：`inbox_url`、`inbox_principal_id`、`inbox_key_env`（I2：三者齐备才可接收）、`coord_secret_env`。planner 保留（role=planner），worker 行 role=worker |
| `PeerEdge` | 新表 `peer_edges(from_bot_id, to_bot_id, created_at, updated_at)` | 显式授权边。planner→已启用 worker 保持隐式（注册即授权，现状）；worker↔worker 默认拒绝（I3）。同步模式 `allowed_callers` 不复用 |
| `ConversationRoot` | `tasks` 表加列 `conversation_root`、`hop`、`kind` | UUID，串起一次多 Bot 协作。用户首个委派建根（hop 0）；peer 转派继承根并 +1（I4：根与 hop 在 HTTP 前落库） |
| `HopBudget` | 协调器准入校验 | 每 root `max_hops`（默认 4）、每对 Bot 单位时间消息上限、扇出上限（sender 未结 peer 任务数）、并发上限（recipient 队列）。超限返回类型化拒绝（I6），事件含 sender/recipient/task/hop |
| `PeerTask` | `tasks` 表 `kind` 区分 | peer 任务复用现有 dispatch outbox、幂等 operation key、远端观察与额度机制；新增 sender/recipient/root/hop 字段。先持久再发送（I4）已由现有事务保证 |
| `DeliveryReceipt` | 接收方宿主 inbox（现有） | receipt 固定 Task+run+instance；重复投递幂等（现有 §9 协议）。peer 场景接收方 = 发起方 Bot 自己的宿主（I5） |

## 3. 方案对比（目标 P1 要求至少三种）

| 方案 | 内容 | 判定 | 理由 |
| --- | --- | --- | --- |
| A. 每 Bot inbox 绑定 + runs 协议 | bots 表扩列做绑定表；每个 Bot 的宿主单进程同时挂 runs + inbox（ADR-001）；capability 由接收方宿主铸造、协调器只转发；结果投递回发起方 inbox | **选定** | 完全复用现有 run/inbox/receipt 协议（目标 §2 明令）；I2/I5 天然满足（capability 铸造在接收宿主，`gateway_runtime.py:153-182` 的铸造本就通用、绑 session+generation+workflow）；改动集中在协调器 + 一处最小宿主入口 |
| B. 协调器代持 standing capability | 注册时向每个 Bot 索取长效 capability，存协调器，代收代发 | 拒绝 | 违反 I2/I5（协调器不能代铸/代持）；长效秘密入库扩大爆炸半径；撤销与过期语义都更弱；等于把协调器变成第二宿主 |
| C. 宿主级消息总线 | nanobot 核心加 peer 通道/发布订阅，Bot 直连互通 | 拒绝 | 大面积改核心，违反 I9 最小宿主改动；与协调器的 task/outbox/receipt 所有权表（架构文档 §3）冲突；Bot 直连 mesh 仍需每 Bot 服务端与认证，问题没消失只是搬家 |

## 4. 派发与回传流（目标态）

1. 秘书长（planner）在用户会话中经 `submit_task` 派活给 worker（现有路径，隐式边，建 conversation root）。
2. worker 执行 runs 时经 MCP 调 `send_to_bot(target, input, operation_key)`（新工具，见 §5）；其宿主经 trusted meta 铸造 `reply_capability`（绑 worker 自己的 session+workflow），协调器校验 sender 凭据、`sender→recipient` 边、hop/扇出/并发/窗口预算后落库 peer task（I4），复用 dispatch outbox 发 `/v1/agent-runs`。
3. recipient 完成 → 协调器把结果投递到 **sender 的 inbox**（bots 表 sender 行的 inbox_url + inbox_key_env），capability 是 sender 宿主铸造的原值（I5）。sender 宿主收件、去重、唤醒 sender 会话（现有 pending_user_turn / owner 语义，R2–R5 已修的链路）。
4. sender 会话续跑：可轮询（`get_async_task`）或被唤醒后汇总，再经 `send_to_bot` 向秘书长回报（peer 边），或以原 run 结果回传（若在 run 期限内完成）。
5. 用户只与秘书长对话（I7）：worker 的 peer 轮次进 worker 自己的会话，永不注入用户会话；秘书长收到汇总后产生用户可见轮次。

崩溃收敛（I8）全依赖现有机制：任务与正文先持久再发 HTTP、outbox 重读、receipt 幂等、tombstone、重复 operation key 幂等。

## 5. 工具与接口决定

- **新工具 `send_to_bot`，不改 `submit_task`**（目标 P4 二选一）：`submit_task` 是 project/workbench 域、planner principal、隐式边；peer 派发是 conversation 域、bot principal、显式边 + hop 信封 + 预算校验。混用会让 planner 路径背上不适用它的 peer 不变量，也会让 workbench 任务伪造 hop 语义。两者共享底层 task/outbox 机制。
- **协调器新路由**：`POST /v1/teams/peer/send`（Bot principal 专用；`send_to_bot` 的后端）、`GET /v1/teams/usage?window=7d|30d|all`、`GET /v1/teams/bots/{bot_id}/usage`。事件流复用现有 events。
- **缓存统计读取器**（P6）：`nanobot_teams/coordinator/usage.py` 直接只读打开 `<bot_homes_root>/<bot_id>/llm_usage.sqlite3`（URI `mode=ro`、`PRAGMA query_only=ON`、busy timeout）。不导入 `nanobot.*`（Teams 包边界），不写 Bot 库，不读正文。schema `user_version=1` 校验；文件/表/版本异常返回 `available:false` + 原因。公式按目标 §5（`llm_usage/store.py:75-80` 已是 observed 分母语义，读取器复刻该语义并加 per-model 拆分与 7d/30d/all 窗口；无 bot 维度列——每 Bot 独立 home 库即维度）。

## 6. 威胁模型

| 威胁 | 缓解 | 残余 |
| --- | --- | --- |
| 伪造发送方 | 身份只来自令牌验证（hmac 比较，启动期建 token→principal 映射）；MCP meta 的来源字段由宿主铸造，模型 arguments 中同名键拒绝（`async_mcp.py:325-334` 已有先例）；peer 路由要求 sender 是已验证 bot principal 且在授权边内 | 同机恶意进程窃取环境变量内的令牌（OS 边界，架构文档 §12 已声明） |
| capability 盗用 | `token_urlsafe(32)`，接收宿主只存哈希，TTL + session/generation/workflow 绑定（`external_inbox.py:152-177`）；协调器 outbox 持有原值至投递完成，不进事件/DTO/日志/UI；投递目标固定为 Bots 表绑定 URL，不可被任务参数改写 | 被攻陷协调器可读 outbox 原值（受信控制平面，已声明） |
| SSRF 到 Bot 端点 | `inbox_url`/`endpoint` 注册仅 operator 路由；默认仅 loopback 显式端口清单；复用 URL guard 规范化，禁重定向，控制客户端 `trust_env=False`（R4 已做） | 操作者主动注册内网地址（部署边界） |
| peer 消息提示注入 | 结果按外部任务数据注入，复用 subagent 报告安全语义（不构成授权/审批）；权限不提升；正文有界（32KiB 截断标记） | LLM 提示注入无法根除（架构文档 §12 已声明） |
| 跨 Bot 数据泄露 | 默认拒绝边；正文仅 sender/recipient/operator 可见；工作台事件只含 sender/recipient/task/hop/状态/错误码，不含正文（I10）；结果只进 sender inbox（I5） | 受信协调器本机可见全部正文（单用户 MVP 边界） |
| 环路与放大 | 每 root `max_hops=4`；每对窗口上限；扇出上限；并发上限；全部类型化拒绝 + 审计事件（I6）；peer 任务计入 sender 未结任务额度（现有 32/128 上限族） | 预算内的真实工作放大（属产品语义） |
| 钱包耗尽 | hop 上限约束链长；每 principal 未结任务、每 workflow 次数、队列深度沿用现有额度；peer 派发同样走"事务预留→失败零 HTTP" | 无链级硬费用上限（0.4 以来既定边界，BLOCKERS 已声明） |

## 7. 迁移（P3 预案）

`bots` 表幂等 ALTER ADD COLUMN（inbox_url / inbox_principal_id / inbox_key_env / coord_secret_env，全可空）。现有数据库打开行为不变：旧库无 inbox 列 → 加空列；planner 行为不变（现有 `planner` 配置块继续生效，或物化为 role=planner 行——实现时以"旧库打开后 planner 派发回归全绿"为准）。`principal_for` 改为查表 + 两个角色令牌。`legacy:` 隔离行回填逻辑不动。迁移幂等：重复 open 不重复写。

## 8. 阶段映射

| 阶段 | 本设计落点 |
| --- | --- |
| P2 | 四 Bot 探针先红（含 send_to_bot 不存在、worker 无 inbox） |
| P3 | ADR-001 落地 + bots 表扩列 + principal 查表 + 探针翻绿 |
| P4 | peer_edges + send_to_bot + HopBudget + 类型化拒绝 + 审计 |
| P5 | 结果回发起方 inbox + 唤醒 + 强杀收敛 |
| P6 | usage 读取器 + 两个 usage 路由 + fixture 精确断言 |
| P7 | 工作台 Bot 图 / peer 消息 / 每 Bot 用量面板 |
| P8 | 交付与独立审核 |
