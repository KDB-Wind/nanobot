# Nanobot Teams：架构与实现说明

> 面向外部评审。目标是把「它是什么、为什么这样设计、边界在哪、哪些地方最值得挑」讲清楚，
> 而不是重复 README 的安装步骤。代码基线：本仓库 `main`，版本 `0.3.0`，
> 被测 Nanobot `0.4.1`（commit `1e2dc02f`）。

## 1. 定位

Nanobot Teams 是一个**独立的 stdio MCP 服务器**，让一个 Nanobot 实例（Planner）通过
另一个 Nanobot 实例（Research）的**公开 HTTP API** 委派一个自包含任务，并同步取回结果。

它不改 Nanobot 核心、不 fork、不 import 私有模块、不读 session 数据库。所有能力都建立在
三个公开契约上：

1. `POST /v1/chat/completions`（Nanobot 的 OpenAI 兼容 Agent API）
2. MCP 协议（stdio，工具调用）
3. Agent Plugin 机制（`plugin.json` + `skills/*/SKILL.md`）

配套的 Agent Plugin 只提供技能（skills-only），不含 `mcp.json`。MCP 服务器由用户在
Planner 的 `tools.mcpServers.teams` 中单独注册。

## 2. 范围

| 能力 | 状态 |
| --- | --- |
| 单跳委派、确定性会话、超时与大小上限 | 已实现 |
| 多 Bot 注册表（schema 2）与「谁能调谁」的授权图 | 已实现 |
| 多跳链（信封、hop 上限、环路检测、deadline 递减） | 已实现 |
| 单飞（同一进程同一时刻一个委派）与失败后阻断 | 已实现 |
| 每个 caller 的预算（次数、结果字节、累计秒数） | 已实现，依赖账本 |
| 本地 SQLite 账本、`list_tasks` / `get_task` | 已实现 |
| 生命周期：`bot init`、`check`、`graph` / `budget` / `audit` | 已实现 |
| 远端任务查询、取消、恢复、幂等 | **不做**（上游无此 API） |
| 自主群聊、@ 提及、主动推送 | **不做**（需渠道或常驻服务，超出范围） |
| 自动拉起/托管 Bot 进程 | **不做**（生命周期的最后一步由操作者执行） |
| 跨进程预算锁、exactly-once | **不做**（假设每个账本一个写入者） |
| 定义 Bot 的图形界面 | **不做**（核心不提供插件 UI） |

## 3. 运行时拓扑

```text
用户
  │  (WebUI / 渠道 / HTTP)
  ▼
Planner Nanobot 进程 (gateway 或 serve)
  │  工具调用 mcp_teams_delegate_task
  ▼
nanobot-teams MCP 子进程 (stdio, 每个 Planner 一个, --caller planner)
  │  POST http://127.0.0.1:<port>/v1/chat/completions   Bearer <target key>
  ▼
Research Nanobot 进程 (serve)
```

一次委派 = 一次 HTTP 请求。MCP 子进程随 Planner 启动而拉起、随其退出而结束；它不在
两次工具调用之间保存除「忙」「阻断」之外的状态。没有后台任务、没有队列、没有回调。

多跳时，中间的 Bot 也注册同一个 MCP 服务器，只是 `--caller` 换成它自己的 id；
一份 `teams.json` 服务于所有实例，每个实例只解析自己有权访问的目标的密钥。

## 4. 七个关键决策

**D1 插件只放技能，MCP 走 `tools.mcpServers`。**
Agent Plugin 的 MCP 条目只接受 `type/command/args/env/cwd`，无法设置 `toolTimeout`；
Nanobot 对通用 MCP 的默认工具超时是 30 秒（`nanobot/config/schema.py:424`）。
把同一个服务器写成 `mcp.json` 会让 35 秒以上的任务稳定失败。所以插件只负责「让模型知道
有这个能力」，服务器由用户在自己的配置里注册，并显式写 `toolTimeout: 180`。

**D2 会话 id 由 (team, caller, target, thread) 决定，而不是每次任务新建。**

```text
sha256(json([schema_version, team_id, caller_id, target_bot_id, thread_key]))[:32]
→ "teams-v1-<32 hex>"
```

这样同一个团队线程对同一个目标复用同一段远端上下文，任务之间能接上；代价是**并发任务会
共享远端会话**（本实现用单飞避免这一点）。

**D3 单飞 + 永久阻断。**
同一进程同时只允许一个委派；第二个立即返回 `busy`，不排队。任何「可能已经到达远端但结果
不明」的结局（超时、5xx、响应无法解析、传输中断）都会把进程置为 `blocked`，后续委派不再
发出 HTTP，直接返回 `blocked`。这个标志只在内存里，MCP 重连会清掉。

**D4 结果只分「拿到了」和「没拿到」，不假装知道语义。**
`status=completed` 只表示响应结构合法；`semantic_status` 恒为 `unverified`。
服务端无法判断研究内容是否正确，这一步留给 Planner。

**D5 信封是「上限」，不是「认证」。**
多跳字段经由远端模型转述，中间 Bot 可以改写。服务端只做本地不变量检查（hop 上界、链形状、
环路、deadline 只能变小），不声称能证明调用链的真实性。

**D6 账本只记观察。**
每一行是「本地进程提交了什么、看到了什么」，不是「远端最终做了什么」。因此没有内容、
没有提示词、没有密钥，只有 id、状态、字节数、耗时。

**D7 生命周期工具不托管进程。**
`bot init` 生成目录与注册表条目并打印启动命令，是否启动、用什么账户启动由操作者决定。
这避免了在插件里引入进程管理器、PID 文件和端口回收逻辑。

## 5. 模块地图

| 文件 | 行数 | 职责 | 明确不做 |
| --- | --- | --- | --- |
| `config.py` | 426 | 解析并校验 `teams.json`，解析密钥，解析 caller | 不读密钥值以外的环境变量 |
| `envelope.py` | 209 | 多跳信封：生成、校验、渲染、解析 | 不认证上游 |
| `transport.py` | 231 | 一次 HTTP：构造请求、状态映射、有界读取、截断 | 不重试、不跟随重定向 |
| `delegation.py` | 309 | 授权、顺序、单飞、阻断、预算、账本写入、日志 | 不排队、不缓存结果 |
| `ledger.py` | 225 | SQLite 追加写与只读查询 | 不存内容、不更新旧行 |
| `server.py` | 231 | 四个 MCP 工具、`isError` 语义、stdio 生命周期 | 不做业务判断 |
| `lifecycle.py` | 402 | `bot init`、`check`、端口探测、版本比对 | 不启动进程 |
| `reporting.py` | 127 | `graph` / `budget` / `audit` 的文本视图 | 只读，不建库 |
| `__main__.py` | 220 | 子命令分发与参数解析 | — |
| `agent-plugin/...` | — | `plugin.json` + `SKILL.md` | 不含 `mcp.json` |

## 6. 配置模型

`teams.json` 有两个版本，都归一化成同一个 `TeamConfig`。新部署应使用 schema 2。

### 6.1 schema 2 结构

```json
{
  "schema_version": 2,
  "team_id": "research-team",
  "thread_key": "main",
  "bots": [
    {"id": "planner",  "display_name": "Planner",  "role": "plans and writes", "allowed_callers": []},
    {"id": "research", "display_name": "Research", "role": "gathers sources",
     "capabilities": ["search", "summarize"],
     "model_preset": "step-5-preview",
     "endpoint": "http://127.0.0.1:8901/v1/chat/completions",
     "api_key_env": "TEAMS_RESEARCH_API_KEY",
     "allowed_callers": ["planner"]}
  ],
  "limits": {"max_hops": 2},
  "budgets": {"planner": {"delegations": 20, "result_bytes": 200000, "chain_seconds": 600}}
}
```

### 6.2 校验规则（启动即失败，不做猜测）

- 顶层与 Bot 的未知键一律拒绝；`bots` 至少一个。
- Bot id 匹配 `^[a-z0-9][a-z0-9._-]{0,63}$`；重复 id 拒绝。
- **有 `allowed_callers` 的 Bot 必须同时有 `endpoint` 与 `api_key_env`；
  没有 `allowed_callers` 的 Bot 不允许有这两项。** 这条把「可被调用」与「可被寻址」绑定。
- `allowed_callers` 不能包含自己，也不能包含未注册的 id。
- `endpoint` 必须是 `http://127.0.0.1:<port>/v1/chat/completions`：只允许 http、
  只允许回环地址、必须显式端口、路径必须完全一致、不允许 userinfo/query/fragment。
- `capabilities` 最多 8 个，短小写 token；`model_preset` 是可选的展示元数据。
- `budgets` 只能指向已注册的 Bot，值必须为正；`delegations` / `result_bytes` 必须是整数。

### 6.3 限制默认值

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `task_max_bytes` | 16384 | 委派任务的 UTF-8 字节上限 |
| `response_max_bytes` | 262144 | 读取远端响应体的上限 |
| `result_max_bytes` | 32768 | 返回给 Planner 的内容上限（按 UTF-8 边界截断） |
| `connect_timeout_s` | 3.0 | TCP/TLS 连接超时 |
| `request_timeout_s` | 135.0 | 读写超时 |
| `total_timeout_s` | 145.0 | 单次委派总预算，也是根信封的 deadline |
| `max_hops` | 2 | 链上最多几跳（根为 1，取值 1–4） |

### 6.4 密钥解析

启动时按 `targets_for(caller)` 逐个读取 `api_key_env`，缺任何一个就打印变量名并退出 2。
密钥值只存在于 MCP 进程内存与发往目标的 `Authorization` 头中；不写日志、不进账本、
不进异常文本。

## 7. 请求契约

```json
{
  "messages": [{"role": "user", "content": "<信封 + 任务>"}],
  "session_id": "teams-v1-<32 hex>",
  "stream": false
}
```

- 只发一条 user 消息；不带 system、不带模型名、不带温度。目标用自己的默认 preset。
- Nanobot 服务端会给 `session_id` 加 `api:` 前缀（`nanobot/api/server.py:293`），
  所以远端看到的是 `api:teams-v1-...`，并且按会话串行加锁。
- 客户端：`httpx.AsyncClient(trust_env=False, follow_redirects=False)`，连接 3s、
  读写 135s，外层 `asyncio.timeout(min(145, 信封 deadline))`。
- 只看 `choices[0].message.content` 是否为字符串；其余形状一律视为无效响应。

## 8. 委派执行顺序

`delegate_task` 内的顺序是刻意固定的：

1. 目标是否在授权图内（不在 → `invalid_target`，不发请求）
2. 任务是否为空或超限（→ `task_invalid`）
3. 是否处于阻断（→ `blocked`）
4. 是否忙（→ `busy`）
5. 预算是否用尽（→ `budget_exceeded`；预算已配置但没有账本也在此拒绝）
6. 信封校验（根委派生成，续跳校验）
7. 置 `busy`，**此后到清除 `busy` 之间没有 await 之外的分支**
8. 发 HTTP，得到 `TransportOutcome`
9. 清 `busy`；若 `blocks_future` 则置 `blocked`
10. 写账本（若启用）、写一行结构化日志、返回 JSON

第 1–6 步都在 `await` 之前完成，所以「检查忙 → 置忙」在 asyncio 单线程模型下是原子的。

## 9. 状态与错误码

| 触发 | `status` | `error_code` | 阻断后续 |
| --- | --- | --- | --- |
| 200 且响应形状合法 | `completed` | — | 否 |
| 200 但 JSON 非法/形状不符 | `outcome_unknown` | `response_invalid` | 是 |
| 200 但响应超过 `response_max_bytes` | `outcome_unknown` | `response_too_large` | 是 |
| 401 / 403 | `failed` | `auth_failed` | 否 |
| 其它 4xx | `failed` | `request_rejected` | 否 |
| 503 | `failed` | `provider_unavailable` | 否 |
| 504 | `timed_out` | `target_timeout` | 是 |
| 其它 5xx | `outcome_unknown` | `target_error` | 是 |
| 3xx 等意外状态 | `outcome_unknown` | `unexpected_status` | 是 |
| 连接失败/连接超时 | `failed` | `connect_failed` | 否 |
| 其它传输错误 | `outcome_unknown` | `transport_error` | 是 |
| 本地总预算耗尽 | `timed_out` | `deadline_exceeded` | 是 |
| 目标未授权 | `failed` | `invalid_target` | 否 |
| 任务为空/超限 | `failed` | `task_invalid` | 否 |
| 已有委派在跑 | `failed` | `busy` | 否 |
| 之前留下未知结局 | `failed` | `blocked` | — |
| 预算用尽/无法核对 | `failed` | `budget_exceeded` | 否 |
| 信封非法/超 hop/环路 | `failed` | `envelope_invalid` / `hop_limit` / `loop_detected` | 否 |

MCP 层：除 `completed` 外，所有结果 `isError=true`。参数错误、未知工具、账本查不到
分别返回 `invalid_request` / `unknown_tool` / `not_found`。

## 10. 多跳信封

目标收到的是：

```text
[teams-envelope]
root_task_id: <uuid>
task_id: <uuid>
parent_task_id: <uuid 或 none>
hop: <int>
deadline_remaining_s: <float>
chain: planner,research
[/teams-envelope]

<任务正文>
```

- 根委派：`hop=1`，`chain=(caller,)`，`deadline=total_timeout_s`。
- 续跳：中间 Bot 按 `SKILL.md` 原样复制字段，并把自己的 `task_id` 作为 `parent_task_id`。
- 服务端校验：五个字段要么全有要么全无；`hop >= 2` 且 `<= max_hops`；
  `len(chain) == hop`；`chain[-1] == caller`；目标不在链上；链上每个 id 都已注册。
- 下发给目标的信封：`hop+1`，`deadline * 0.9`，`chain + (target,)`，
  `parent_task_id = 本次 task_id`。deadline 只会变小，永不变大。
- 实际使用的预算是 `min(total_timeout_s, 信封 deadline)`。

**已知弱点**：中间 Bot 完全省略这五个字段时，服务端会把它当成新的根委派（hop=1、满预算）。
服务端无法区分「合法的 Planner」和「想重置链的中间 Bot」。硬约束依赖授权图与每 caller 预算，
README 已写明这一点。

## 11. 预算

- 归属：按 **caller** 计，不按 target 计。`budgets` 的键是 caller 的 id。
- 口径：`delegations` = 账本中该 caller 的行数（**含失败尝试**）；
  `result_bytes` = 结果内容字节和；`chain_seconds` = `duration_ms` 之和 / 1000。
  嵌套时子跳耗时会被父跳再计一次，所以它是「累计等待」而非链墙钟。
- 判定：每次委派**之前**比较 `used >= limit`，因此语义是「最多 N 次」。
- 依赖：预算必须有 `--ledger`。没有账本 → 拒绝；账本读不出来 → 拒绝（fail closed）。
- 不对称：账本**写**失败只记日志、不阻断委派（fail open）；预算**读**失败则拒绝。

## 12. 账本与运维视图

SQLite，单表 `tasks`，只追加：

```text
task_id, root_task_id, parent_task_id, caller, target, hop,
status, error_code, truncated, http_sent,
request_bytes, response_bytes, result_bytes,
started_at, finished_at, duration_ms
```

- 不存任务文本、回答内容、提示词、密钥。
- `started_at` 是墙钟 UTC ISO8601；`duration_ms` 来自 `time.monotonic()`。
- 写失败只记 `ledger write failed`，委派照常返回。
- `list_tasks` / `get_task` 与 CLI 的 `audit` / `budget` 都用只读 URI 打开；
  文件不存在时打印 `missing:`，不会创建空库。

CLI 里 `graph` 输出文本邻接表 + Mermaid；`budget` 显示 `used/limit ok|OVER`；
`audit` 按时间倒序列出最近记录并给出状态计数。

## 13. 生命周期工具

**`bot init`**（要求 schema 2 注册表）：

1. 预检：`role` 非空、端口未被占用、id 未重复、`allowed_callers` 都在注册表里；
2. 读取用户配置里的 preset 与 provider 块，**把 provider 的明文 key 替换成
   `${PROVIDER_NAME_API_KEY}` 引用**；
3. 构造新注册表并整体校验（含 `budgets`）；
4. 校验通过后才落盘 `<home>/config.json` 与 `<home>/workspace`，再写注册表；
5. 打印启动命令与需要导出的环境变量。

生成配置里 `api.apiKey` 是 `${TEAMS_<ID>_API_KEY}`，gateway 端口为 `port + 1000`。

**`check`** 返回 `Finding` 列表（`error` / `warn`）：
注册表非法、caller 不存在、`api_key_env` 未设置、端点有人应答/无人应答、
preset 不在用户配置里、找不到 nanobot CLI、nanobot 版本不等于 `0.4.1`。

## 14. 与 Nanobot 的集成点

| 集成点 | 事实 | 影响 |
| --- | --- | --- |
| HTTP API | `POST /v1/chat/completions`，单条 user 消息，Bearer 认证 | 服务器的全部输入面 |
| 会话 | 服务端加 `api:` 前缀，按会话串行 | 同一 (caller,target) 的任务天然排队 |
| 工具超时 | 通用 MCP 默认 30s（`schema.py:424`） | 必须在注册时写 `toolTimeout: 180` |
| 请求超时 | `api.timeout` 默认 120s（`schema.py:388`） | 35s 任务没问题；>120s 会得到 504 |
| 工具命名 | `mcp_<server>_<tool>` | `mcp_teams_list_bots`、`mcp_teams_delegate_task` |
| 插件 | `plugin.json` + `skills/<name>/SKILL.md`，需在 Apps 里启用 | 只影响模型可见的技能，不影响 MCP |
| 工具注解 | `delegate_task` 标为 `idempotentHint=false`、`readOnlyHint=false` | 上游不会把它当作可重放调用 |

需要强调：插件启用与 MCP 注册是**两件独立的事**。只装插件不会出现工具；只注册 MCP
不会让模型知道何时该用。两者都做，模型才会在合适的任务上调用。

## 15. 安全模型

**信任边界**

- `teams.json`、Nanobot 配置、操作者输入：受信。
- 远端 Bot 的回答：**不受信**（可能包含提示注入）。SKILL.md 要求把它当资料而非指令，
  但最终仍由 Planner 的模型判断。
- 信封字段：不受信的结构化输入，服务端只做本地不变量检查。
- 账本：本地文件，可能被同机其它进程读取；因此不存内容。

**已实现的边界**

- 只能调用 `allowed_callers` 授权的目标；未授权 id 与未注册 id 同样被拒。
- 端点只能是 `http://127.0.0.1:<port>/v1/chat/completions`：阻断 SSRF 到外部主机，
  但不阻止指向本机其它端口（注册表本身受信）。
- 密钥只按 caller 的可达目标解析；Planner 的进程拿不到它无权访问的 Bot 的 key。
- 不跟随重定向；不使用环境代理（`trust_env=False`）。
- 任务 16 KiB、响应 256 KiB、结果 32 KiB 三层上限；截断按 UTF-8 边界，不会切坏字符。
- 日志只含 id、状态、字节数、耗时；不含任务文本、回答、密钥。
- `delegate_task` 被标为不可重放，避免上游在中断恢复时自动重发。

**尚未解决的边界**

- 阻断标志不持久化：MCP 重连即清空，且「重启」不等于「远端已停止」。
- 信封不可认证：省略字段即可重置链（见第 10 节）。
- 单机多实例共享一个账本时没有跨进程锁；预算可能被并发突破。
- 没有针对「远端返回超长但合法」的内容审查，只有截断。

## 16. 测试与验收

**单元与契约**：152 个测试，覆盖配置校验（未知键、端点、图一致性）、信封矩阵、
传输状态映射、单飞与阻断、预算、账本、生命周期、打包（无私有导入、无文件写入、
示例配置可加载）、真实 stdio MCP 客户端往返。

**实机验收**（`acceptance/run_e2e.py`，16 个场景，需要两到三个真实 Nanobot 进程）：

| 场景 | 证明什么 |
| --- | --- |
| `research_direct` | 目标实例的 HTTP API 可用 |
| `delegate_basic` | Planner 经 MCP 取回 Research 的回答 |
| `session_persistence` | 确定性会话 id 在目标重启后仍复用 |
| `long_timeout` | 35 秒任务超过默认 30 秒工具超时仍成功 |
| `unknown_block` | 500 之后置阻断，重试不再发 HTTP |
| `multi_bot` | 三个 Bot 的任意授权对可委派 |
| `unauthorized_pair` | 未授权对被拒 |
| `chain_abc` | A→B→C 多跳成功并回传 |
| `chain_loop` | A→B→A 被拒 |
| `chain_budget` | 链预算耗尽被拒 |
| `bot_init_live` | 生成的配置能真正启动并完成委派 |
| `check_errors` | 端口/密钥/preset 错误能被定位 |
| `ledger_restart` | 重启后可查历史，且不含内容 |
| `budget_reject` | 超预算被拒 |
| `packaging_fresh` | wheel 构建、干净 venv 安装、委派、卸载 |
| `plugin_manifest` | 真实 `discover_agent_plugins` 接受该包 |

**WebUI 路径**（`acceptance/run_webui.py`，Windows + Edge）：Apps 页启用插件 → 新建会话 →
两次工具调用 → 只有 Research 工作区能读到的随机串经 Planner 返回。

**CI**：GitHub Actions，`ubuntu-latest` 与 `windows-latest`，跑 `ruff` 与 `pytest`；
依赖由 `pip install -e ".[dev]"` 安装，ruff 固定为 0.17.4。

## 17. 已知限制

| 限制 | 原因 | 影响 | 缓解 |
| --- | --- | --- | --- |
| 无远端状态/取消/恢复 | 上游 API 没有 run id | 只能同步等待 | 明确文档化，不暴露假接口 |
| 阻断不持久 | 状态在 MCP 进程内 | 重连即清空 | 文档要求人工核对；`list_tasks` 可查 |
| 信封可被省略 | 字段经模型转述 | 中间 Bot 可重置链 | 授权图 + 每 caller 预算 |
| 预算依赖账本 | 无账本无法累计 | 不配 `--ledger` 时配置预算会全拒 | 启动时明确报错 |
| 无跨进程预算锁 | 单写入者假设 | 多实例可能超额 | 文档标注为不支持 |
| 单飞 | 有意设计 | 并发任务返回 `busy` | 由 Planner 串行安排 |
| 一个固定线程 | MVP 约束 | 不适合多租户 | 明确「一个安装一个用户」 |
| 无 Bot 管理 UI | 核心无插件 UI | 只能 CLI 创建 | `bot init` + 文档 |
| WebUI 未在 Unix 验证 | 需要图形浏览器 | 仅 Windows 有记录 | 服务端路径双平台已覆盖 |
| wheel 未发布 | 需发布授权 | 只能源码安装 | 构建与安装已验证 |

## 18. 建议评审重点

如果只挑六处挑战，我会选这些：

1. **会话粒度**：按 (caller, target) 复用一个远端会话，意味着不同主题共享上下文，
   也可能互相污染。是否应该把 `thread_key` 提升为每次任务的参数？
2. **永久阻断是否过重**：一次 5xx 就把进程锁死到重启，代价是否大于「可能重复执行」？
   有没有更细的退避或人工确认机制？
3. **信封的信任模型**：接受「无字段 = 新根」是否是正确默认？是否应要求显式
   `--allow-root` 或注册表开关？
4. **预算口径**：按 caller 计、含失败尝试、时间按累计而非墙钟——这三个选择是否符合
   「防止一条链烧掉全部额度」的初衷？
5. **超时分层**：135s 客户端 / 145s 总预算 / 120s 服务端 `api.timeout`。
   目前 >120s 的任务会先拿到 504 并触发阻断。这层关系是否该在部署时强制校验？
6. **账本 fail-open vs 预算 fail-closed**：写失败不阻断、读失败就拒绝。这个不对称是否
   会在磁盘故障时把系统变成「能跑但不能用」？