# Nanobot Teams 0.4.0：修复后的架构契约

本文描述当前实现。0.3.0 的审核证据保留在 [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md)，修复与验证见 [REPAIR_STATUS.md](REPAIR_STATUS.md)。旧版本的实机记录不算作本次通过结果。

## 1. 定位与信任边界

独立 stdio MCP 进程，通过 Nanobot 公开 `POST /v1/chat/completions` 委派任务。运行时代码不导入 Nanobot 私有模块，不读其 session 数据库，不托管 Bot 进程。注册表、控制状态目录和操作者受信；远端回答与模型转述的信封不受信。一个安装对应一个受信用户。目标实例的长期记忆、workspace 和工具权限不因 session 隔离而隔离。

## 2. 支持范围

实现单跳、显式续接、持久准入与未知结果隔离、caller 生命周期额度、多 Bot 授权图、运维查询、生成目标配置及部署检查。多跳是默认关闭的受信协作模式，只接受无环授权图。远端查询、取消、恢复、幂等、exactly-once、认证调用链、链级硬费用预算与多租户隔离没有提供。

## 3. 运行时拓扑

```text
Planner Nanobot
  -> Teams stdio MCP（固定 caller、持久账本、一个写入者）
    -> Research Nanobot HTTP API
```

每次委派是一条同步 HTTP 请求。SQLite 控制状态和目标隔离跨调用、跨 MCP 重启保留。没有后台执行队列。插件只放技能，MCP 单独通过 `tools.mcpServers` 注册并设置超时。

## 4. 状态所有权

| 所有者 | 职责 |
| --- | --- |
| config.py | 注册表、端点规范化、DAG 与静态深度、caller 权限、有限数值边界 |
| ledger.py | 准入事务、额度预留、待定任务、终态结算、持久隔离、scope、人工确认事件 |
| file_lock.py | Windows 和 Unix 的单写入者锁，进程结束由 OS 释放 |
| delegation.py | 单飞、输入校验、会话、信封、取消收尾、结果与有界日志 |
| transport.py | 一次 HTTP、错误分类、有界 raw 读取、JSON 与 Unicode 校验 |
| server.py | 四个 MCP 工具、参数 schema、isError、资源生命周期 |
| lifecycle.py | 生成配置、凭据外置、注册表锁与提交、目标和声明期限检查 |
| reporting.py、__main__.py | 运维视图、本机 acknowledgment、启动 |

生命周期不再依赖进程内全局 blocked 布尔值。`TransportOutcome.blocks_future` 由状态推导，unknown 和 timed_out 必然隔离。

## 5. 注册表与启动

schema 1 为单跳兼容格式；schema 2 增加 `allow_multihop` 和 Bot 的 `can_start_root`。未知键拒绝。空 `allowed_callers` 与省略该字段都表示不接受 caller。可寻址目标必须有非空 caller 列表、endpoint 和 api_key_env；不接受 caller 的 Bot 不设置端点。

图必须为 DAG。多跳默认关闭；启用时最长静态路径不得超过 max_hops。根权限可显式授予或拒绝；未声明时，图源节点可开始根任务，接收委派的节点不可以。新配置建议显式声明。注册表最多 64 个 Bot。

默认账本为注册表目录 `.teams-state/<caller>.sqlite3`，也可显式 `--ledger`。启动须取得锁、完成迁移并绑定 `(team_id, caller)`。不同 scope 不能复用；第二写入者在提供服务前退出。直接构造无持久账本的 Delegator 时，委派返回 ledger_unavailable。

## 6. 会话

新任务生成独立 thread_key。session 哈希包含 schema、team、caller、target、安装级 thread namespace 和此次 thread_key。目标加 `api:` 前缀并串行会话请求。哈希只命名会话，不认证身份。

省略 thread_key 表示新任务；传入之前返回的 key 表示续接。账本须证明同 team、caller、target、规范化 endpoint 和曾有 completed 观察。线程归属持久化。目标保留 home 重启后可延续。可信上游用户身份仍需要宿主支持。

## 7. 执行顺序

1. 校验授权、任务、单飞、持久控制状态和线程参数。
2. 生成根信封，或按显式多跳模式校验 continuation。
3. 单个 SQLite 事务核查隔离与额度、验证线程、裁剪本次字节与等待上限、插入待定任务和 admitted 事件。
4. 设置 busy，扣除准入已耗时间后发送 HTTP。若发送前已到期，记录明确未派发的失败。
5. 收到观察后结算。取消和异常也经过同一收尾；外部取消在同步持久化 unknown 后继续传播。
6. 写终态与事件，再释放 busy。终态写失败保留待定行，并在当前进程隔离端点。

第 3 步之前没有 HTTP。待定行在发送前已保守标记“可能提交”。重启将未结束的 submitted 状态恢复成 interrupted unknown，保持隔离而不重放。

## 8. 账本

tasks 是可更新的状态表，events 追加 admitted、observed、unknown、recovered_unknown、acknowledged_duplicate_risk 事件，metadata 绑定 scope。字段含任务与链 ID、caller、target、endpoint、线程、session、状态、隔离、字节、墙钟、观察耗时和预留计费量。没有任务文本、回答或密钥。人工确认事件另含本地账户和有界单行理由。

SQLite 使用 WAL、synchronous=FULL 和有限锁等待。派发依赖事务成功，不再吞掉关键写入异常。数据库故障属于 ledger_unavailable。运维读取失败显示 unverified，不伪装成零用量。MCP list/get 在运行连接上只做 SELECT；CLI reporting 用只读 URI，不创建缺失库。

## 9. 预算

预算是账本 scope 内每 caller 的生命周期额度，无周期重置。delegations 计已准入次数，包含准入后的失败；本地拒绝仅记有界日志。result_bytes 限制本次返回内容，不是响应体或费用。chain_seconds 是兼容名，计本地预留与结算的等待量，不是链墙钟。

先预留，再执行和结算。字节与本地 timeout 受余额约束。明确观察后归还未用额度；unknown 保留整笔预留，人工确认也不退款。毫秒观察向上取整，避免短请求被向下舍入为零；计费不超过预留时间。父子等待跨 caller 汇总会重叠。本地放弃后远端继续消耗的资源无法由账本证明。

不使用可伪造的 root_task_id 实现所谓链级硬预算。可信 root 的跨节点计费需要新的准入契约。

## 10. 失败分类

| 事件 | 结果 | 隔离 |
| --- | --- | --- |
| 合法 200，观察落盘成功 | completed，semantic_status=unverified | 否 |
| 响应 JSON、深度、Unicode、形状、大小或编码不支持 | outcome_unknown | 是 |
| 401、403 | failed / auth_failed | 否 |
| 已知 API 的 400、404、413 拒绝 | failed / request_rejected | 否 |
| 有界 503 且 error.type=configuration_error | failed / provider_unavailable | 否 |
| 其他 503 和其他 5xx | outcome_unknown / target_error | 是 |
| 504 | timed_out / target_timeout | 是 |
| 连接建立失败 | failed / connect_failed | 否 |
| 可能发送后的传输失败、取消、未分类异常 | outcome_unknown | 是 |
| 本地 HTTP 等待期限到期 | timed_out / deadline_exceeded | 是 |
| 其他状态，包括未知 4xx | outcome_unknown / unexpected_status | 是 |
| 准入失败 | failed / ledger_unavailable，未发 HTTP | 否 |
| 观察落盘失败 | outcome_unknown / ledger_unavailable | 是 |

非 completed 结果均 isError=true。未知工具、非法参数、未找到任务分别为 unknown_tool、invalid_request、not_found。委派注解 readOnlyHint=false、idempotentHint=false、destructiveHint=true。当前宿主不会自动安全重放它，但注解不能阻止模型发起新调用或用户直接访问目标。

## 11. 隔离与人工恢复

隔离按规范化 endpoint，别名不能绕过。独立目标继续工作。旧 unknown 行没有 endpoint 时，保守阻断全部目标。重连不能清除历史不确定性。

操作者停止 MCP 写入者、检查远端效果后，使用 `ack-unknown --ledger ... --task-id ... --reason ...` 接受剩余重复风险。记录账户、理由、时间，保留 unknown 与已计费用量。MCP 不提供解除工具。它不是远端取消，不会把 unknown 改成 failed；每条隔离记录需分别确认。

## 12. 多跳信封

续跳需要 parent_task_id、root_task_id、hop、deadline_remaining_s、expires_at、chain 六字段。expires_at 为 Unix 时间。校验有限数、未过期、hop、链形状、已注册 ID、历史授权边、根权限、目标未出现在链上。无字段不再让中间节点开始新根。

有效预算为本地总限额、caller 剩余时间、声明 remaining、expiry 减当前时间的较小值。下发时基于实际有效预算保留 10% 回程余量。中间推理和排队消耗从 expiry 扣除；本地 timeout 独立限制本次等待。

模型能伪造字段，墙钟也可能调整。这不是认证链或硬分布式期限。没有签名、一次性票据、重放防护和可信 root 计费。DAG 和 caller 准入是服务端约束，信封仍是受信协作提示。

## 13. HTTP 与超时

httpx 禁用环境代理与重定向，一条 user 消息，stream=false，目标用自己的默认 preset。端点仅允许规范化回环 HTTP API。默认 connect=3s、读写=135s、总限额=145s，每次按余额和 expiry 缩短。

Accept-Encoding=identity，拒绝压缩体。有界 raw chunks 在扩展缓冲前检查上限。默认任务 16KiB、响应 256KiB、结果 32KiB；按 UTF-8 边界截断。捕获深层 JSON 和无效 Unicode。超时须有限且不超过一天，字节配置不超过 16MiB。

父仓库 API 补丁将会话锁等待纳入 request_timeout，覆盖普通和流式执行。它不保证外部副作用可撤销。MCP timeout 足够大不代表 Planner 上游入口也会等到结果。

## 14. 部署检查

check --caller 检查可达目标。TCP 探测只说明端口应答，不认证进程。preset 和 CLI 版本是操作者本地信息，不冒充远端实际状态。

部署声明可列出 mcp_tool_timeout_s、planner_timeout_s，以及目标 api_timeout_s、queue_included。检查器验证每层至少 5 秒余量。缺声明显示 timeouts_unverified；有声明也标为操作者声明，不认证远端配置。预算视图默认发现 .teams-state 下账本，拒绝跨 team 或不可验证的汇总。

## 15. 生命周期与安全

bot init 锁定注册表，验证新 home、端口对、ID、环境变量名碰撞、源 preset 和完整新注册表。连接与 preset 仅复制白名单。凭据承载字段的字符串外置为环境变量；既有合法引用保留，未知嵌套 API 字段不复制。默认限制 workspace、关闭 exec、MCP 为空，目标 API 绑定回环并有独立 key。

临时文件 flush、fsync 后 replace，注册表最后提交。普通提交失败回收新建 home；崩溃可能留下未注册的孤立 home，需要核查。没有跨文件事务，不覆盖现有配置，不启动进程。Windows ACL、受保护父目录与账户隔离由部署者提供。

回环地址不防本机端口替换；target-wide key 不认证 caller；提示注入仍可能驱使 Bot 使用已有工具权限。控制文件不得处于模型可写路径。实际出站、工具授权与费用应在目标执行边界落实。账本不存内容不代表目标或模型供应商不留存。Nanobot API 成功日志改记 text_chars；Teams 不记录非法 target 原始值。

## 16. 验证

单元与契约测试覆盖额度、迁移、故障、协议和生命周期。真实 stdio 测试发取消通知、强杀进程并重启、尝试第二写入者。父仓库有队列 deadline 回归。acceptance/repair_live.py 使用真实 Nanobot、glm-5.3-flash 和隔离 home 验证配置、委派、续接与重启。

具体结果见 REPAIR_STATUS.md。历史探针保留 baseline 漏洞断言，修复后的期望由正式回归测试表达，不再要求旧断言通过。

## 17. 升级

停止旧写入者，备份 SQLite 主文件与 sidecars，保留原显式账本路径。混合 caller 账本不能自动绑定；旧 unknown 无 endpoint，需核查后逐条确认。旧无账本安装的未决请求无法补建。旧 session hash 不自动兼容新线程。循环图、默认多跳和重启解锁已取消。

没有发布到外部服务，也不自动修改正在运行的 Bot。只有使用 patched API 的目标才具备包含排队的服务端期限；声明文件不能代替版本核实。

## 18. 六个问题的本版决策

| 问题 | 决策 |
| --- | --- |
| 会话粒度 | 默认隔离，显式且有 scope 归属的 thread_key 续接，不声称多租户隔离 |
| 永久阻断 | 持久 endpoint 隔离，其他目标可用；核查后人工确认，禁止自动重放 |
| 信封默认与信任 | 默认关闭多跳；根权限、DAG、expiry、本地额度生效，信封仍非认证链 |
| 预算口径 | caller 生命周期准入，预留结算；等待与费用、链墙钟分开 |
| 超时分层 | API 锁等待计入期限；部署声明检查目标、客户端、MCP、Planner 入口，未知项明确 unverified |
| 账本 fail-open | 执行控制 fail-closed；发送前持久预留，未知保留额度，写失败不继续派发 |


## 19. Peer 互通与每 Bot 用量（0.6）

目标契约：[PEER_COMMS_GOAL.md](PEER_COMMS_GOAL.md)；设计定稿（ADR-001、方案对比、威胁模型）：[PEER_COMMS_DESIGN.md](PEER_COMMS_DESIGN.md)。本节只描述行为契约。

身份与绑定。每个 Bot 的协调器凭据只在环境变量里（`bots.coord_secret_env` 引用），令牌验证把 `bot:<id>` 识别为自己的 principal；除读取/查询/取消自己的任务外，一切现有路由对 bot principal 默认拒绝。接收绑定（inbox URL、principal、密钥引用）三者齐备才可接收回传（I2）；字段在注册与 scaffold 时校验为 loopback。

授权边与预算。`peer_edges` 表显式记录 worker 间授权（operator 专属路由管理），默认拒绝；planner 派发保持注册即授权。peer 准入在单事务内执行：边检查、4 跳上限、同会话收件人重入即环路（A→B→A 拒绝）、每对每小时 20 条窗口，加上既有配额（扇出=sender 未结任务、并发=recipient 队列）。超限返回类型化错误码并写审计事件（sender/recipient/code/hop，无正文）。

回传（I5）。带 reply_capability 的 peer 任务先经发起方自己的 inbox 校验 capability，终态后投递到发起方绑定地址；`serve` 开启 `api.runs.inboxEnabled` 时（ADR-001，默认关）一个进程同时执行 runs 与接收回传，运行执行器为持久会话 `peer:<workflow_id>` 绑定 PeerConversationBinding，capability 与唤醒都落在这个会话上，ephemeral run 会话保持隔离。

用量与缓存命中率。协调器只读打开 `<bot_homes_root>/<bot_id>/llm_usage.sqlite3`（URI mode=ro + PRAGMA query_only）。命中率 = sum(cache_read_tokens) / 报告了缓存字段的调用的 sum(input_tokens)；缺失为 null（不补 0%）并给 coverage；窗口 7d/30d/all，按模型拆分；库缺失或 schema 版本未知返回 available=false 与原因。金额与凭据不出现。

验收入口。`acceptance/peer_comms_probe.py`：真实四 Bot 拓扑 + 真实模型全链、无权边与第五跳类型化拒绝、同 key 幂等、协调器强杀重启收敛、事件路由字段、真实用量数字；退出码 0 才算过。
