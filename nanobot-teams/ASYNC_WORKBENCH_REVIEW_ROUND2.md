# R0–R5 修复后的第二轮审核

日期：2026-11-09，America/Los_Angeles。审核基线为当前工作区，HEAD `64f0d46692f72fb49929250b295501937a25895e`，保留原有未提交内容。比较对象为上一轮 HEAD `2e9c28cf4` 与 [第一轮报告](ASYNC_WORKBENCH_REVIEW.md)。提交者的部分证据采用 UTC 2026-11-10 时间。

## 1. 结论

仍不通过完整交付验收，但不能沿用“修复没有效果”的判断。真实 MCP meta、常规 Planner 唤醒、公开结果读取、同草稿重试、Bot 客户端边界、writer 锁、常规子进程回收和生成 JSON 已有实质修复。新 R5 脚本也确实增加了 Planner 与 MCP 拓扑，强于上一轮两个独立 Bot 的验证。

剩余阻断集中在状态交接与并发窗口：首次请求在途时仍会确认假取消；wake 在内存排队后丢失持久恢复义务；旧 generation 或撤销后的 workflow 仍可调用模型；活跃轮次收到的报告没有进入模型上下文；抗取消子任务和异常退出仍能留下活工作；重复隔离失效；迁移后的未绑定实例仍绕过 unknown 阻断。不能以 R0–R5 的 done 标记代替这些行为的修复。

本文列出 10 组 P1 与 7 项 P2，每项均有主审核代理独立复跑的当前代码证据。相同根因的多个交错场景放在同一组，不把旧断言因为修复而失败误判为回归。本次没有修改产品实现、已有测试或原进度声明，只新增本轮审核文档与反例工件。

## 2. 本轮实际检查

| 检查 | 本轮结果 | 证据含义 |
| --- | --- | --- |
| Teams 现有测试 | `pytest nanobot-teams/tests -q`，245 passed | 修复后的现有断言通过 |
| 所选 Nanobot 测试 | `pytest tests/runs tests/session/test_external_inbox.py tests/session/test_inbox_continuation.py tests/tools/test_mcp_teams_meta.py -q`，82 passed | 包含本次修复的运行、inbox 和 MCP 回归 |
| 原 UI 两项反例 | 2 passed，pending `calls=1/distinctKeys=1`，重试 `calls=2/distinctKeys=1` | 原双击与相同草稿丢响应问题已消除 |
| 新 UI 项目维度反例 | 旧两项仍过，新一项失败 | 改项目后错误复用旧 key，见 M07 |
| 本轮运行反例 | 真实 AgentLoop、离线 provider、真实子进程、SQLite 和本机 HTTP | 主代理逐项执行下列脚本并核对输出 |
| 安装产物核对 | 当前 0.5.0 wheel 的 async_mcp、Coordinator store/config/http_api 四个模块与源码逐字节相同 | 这些模块不是旧 wheel 残留；不替代全新安装验收 |
| R5 四份 JSON 与脚本 | 阅读并核对拓扑与断言 | 是提交者的 live 证据，本轮没有重新调用真实 GLM，不改称本轮 live 通过 |

使用 interrogate 与 Prove It Works，只保留已运行并违反契约的反例。三个新上下文审查 runs、Coordinator、inbox，主代理审查 UI、证据与工件并独立复跑发现。模型均为 GPT 家族，不声称跨家族复核。没有操作用户现用进程或真实数据，反例创建的子进程和抗取消任务均自行回收。没有测试恶意报告是否能诱导模型执行外部动作，也没有做 4 小时或 8 小时耐久运行。

## 3. 上轮修复的对照结论

| 上轮范围 | 已确认的正常路径 | 本轮仍未闭合的范围 |
| --- | --- | --- |
| F01、F11 | stdio meta 能传递，自动报告携带原 workflow，模型不能改写宿主根 | wake 实际消费与活跃轮次交接，N02–N05 |
| F02、F10、S03、S04、S12 | 空闲 Planner 收报告后调用一次模型，模型可见非授权包络；晚到 HTTP 为 card_only；改绑为 409；无后台能力不 mint | crash、替换会话、撤销后 wake、活跃输入注入，N02–N05 |
| F03 | worker 尚未开始提交前取消，outbox 能关闭 | 第一次 HTTP 已在途，N01 |
| F04 | 原 stop_confirmed=false 被保留；常规成功路径回收普通 exec 子进程 | 抗取消子代理与异常分支，N06 |
| F05、F06 | writer 锁生效，Nanobot 重启后能持久隔离并阻止队列 | Coordinator 再次隔离、终态风险及首次迁移绑定，N07–N09 |
| F07、F08、S08、S10 | 一次结果 503 能重取，公开结果和 MCP include_result 可用，截断保留，清理后不可用性正确 | Nanobot 结果落盘失败，N10 |
| F09 | 同项目同草稿的 pending 和失败重试复用 key | 项目变更未进入草稿身份，M07 |
| F12、F13 | 直接构造有效配置时生成 JSON 可加载并启动，浏览器不再收原始 key | 正式 Coordinator 配置 loader 无法启用该功能，M05 |
| F14、F15、S05、S09 | BotRunClient 禁代理并拒绝压缩和超限；runs 坏 JSON 为 4xx；SSE 过期为流前 410 | MCP CoordinatorClient 读取边界，M04；inbox revision 解析，M06 |
| F16、S01、S06、S11 | 未确认迁移额度能阻断；已预绑定实例的隔离与别名额度有效；未确认运行正文和行保留；普通入口先验证 generation | N04、N08、N09、M02、M03 所列旁路 |

此表是对特定可观察路径的确认，不把一个路径通过扩大为整个编号的所有并发窗口已关闭。原探针断言旧缺陷，新代码让旧断言失败时，应读取实际状态再判断。

## 4. P1 发现

### N01. 第一次 HTTP 已发送，取消仍按 attempts=0 确认未执行

[CoordinatorStore:920](src/nanobot_teams/coordinator/store.py#L920) 用 attempts=0 判断从未发送，但 [worker.py:183](src/nanobot_teams/coordinator/worker.py#L183) 在 submit 之前没有持久发送标记。attempts 仅在失败后增加，不能表示请求是否已在途。

`coordinator/probe_round2.py` 的 cancel_race 在真实 HTTP handler 已收到 POST 后阻塞响应，再执行取消，得到 `attempts_while_submit_in_flight=0, cancel_response=confirmed, stop_claim_while_in_flight=true`。释放响应后为 `submits=1, cancels=0, final_dispatch=accepted, final_run=cancelled, observable=0`。远端执行了，调用方看到的却是已取消，且本地停止观察。

修复需要在可能发送前原子提交 sending 或等价事实，取消在途任务时按原 key 对账并向实际 run 请求停止。所有 awaited preflight 返回后重新检查取消和隔离状态。不能把重试次数换成发送证据，也不能让晚到受理响应覆盖本地取消却不驱动远端收尾。

### N02. wake 只进内存队列就清掉持久义务，进程崩溃后无法恢复

[inbox_wake.py:82](../nanobot/session/inbox_wake.py#L82) publish 到内存 bus 后立即调用 mark_wake_queued，[external_inbox.py:599](../nanobot/session/external_inbox.py#L599) 将 wake_due 清零，此时轮次尚未 dequeue 或持久准入。

`inbox/probe.py crash_queued` 启动独立子进程，在 queued=1、bus_size=1、due=[] 后 kill，随后重开相同会话存储并实际调用 RecoveryCoordinator.scan。结果为 `recovery_pending=[], recovered_wakes=0, planner_calls=0, receipt=injected, recovery_metadata=null, runtime_checkpoint=null`。

这不是宿主要求确认恢复的正常行为，因为连可确认的恢复记录都没有。修复应使义务持续到会话状态所有者持久接收及给出处理状态，按 delivery ID、generation 和 workflow 去重。重启可遵守宿主确认策略，但不能让尚未执行的继续任务消失。

### N03. wake 没有绑定原 generation，也不重新检查撤销许可

[wake_due_deliveries:581](../nanobot/session/external_inbox.py#L581) 不返回原 generation。[InboxWakePump:57](../nanobot/session/inbox_wake.py#L57) 只检查同名 session 是否存在，构造消息时没有 generation，也没有检查 workflow 当前 grant。

`replaced_generation` 先保存报告，再删除并重建相同 key 的会话。旧 wake 仍 queued=1，真实模型在新 generation 被调用一次，看到 `NEW UNRELATED INTENT`，source_workflow 却是旧 wf-root。`revoked_after_inject` 则先应用报告，再处理真实新用户消息撤销 grant，随后仍产生 `extra_planner_calls=1` 的旧 workflow 调用。

修复应从持久报告保留原 generation，在调度和实际消费两个边界校验来源与当前许可。失效 generation 为 orphaned，撤销后的待继续任务为 card_only 或相应结束状态，不能仅检查 session key 是否仍存在。

### N04. 活跃轮次中的真实用户输入走注入路径，绕过撤销

撤销位于 [loop.py:1911](../nanobot/agent/loop.py#L1911) 的新轮次入口，但活跃轮次接收用户消息会走 [_drain_pending:1144](../nanobot/agent/loop.py#L1144)，没有相同的 generation 校验后撤销逻辑。

`injected_user_revocation` 使用真实 agent.run。第一轮 provider 正在等待时发布真实 websocket 用户输入 `NEW UNRELATED INTENT`。第二次 provider 调用已看到新输入，但 `old_workflow_grant=true`；之后旧报告仍 `late_receive=received, late_apply=injected, wake_due=1`，而不是 card_only。

修复应在共享的“接受真实用户输入”边界执行规定的撤销，覆盖新轮次和当前轮次注入，不能只修 process_direct 或 _process_message。保持与 generation 校验的顺序，不再次引入旧 S11。

### N05. 报告在 Planner 活跃时到达，模型只收到 wake 文案而没有报告

[inbox_wake.py:62](../nanobot/session/inbox_wake.py#L62) 只发送泛化通知，依赖下一轮重新读取历史；但 [loop.py:1006](../nanobot/agent/loop.py#L1006) 和 :1132 会将它注入尚未结束的轮次。该轮次的模型 transcript 早于另一路持久历史插入，没有同步到报告正文。

`active_result` 用真实 AgentLoop 和可控离线 provider，在第一请求阻塞期间完成外部报告并 pump wake。得到 `planner_calls=2, first_saw_report=false, second_saw_report=false, second_saw_wake=true, history_report_count=1, due=[]`。唯一的继续义务已经消耗，模型仍未收到报告。不能用“历史有一条记录”证明模型实际消费了结果。

修复可将外部结果设为不能被普通中途注入消费的独立 FIFO 轮次，或经已有 runner 注入协议明确携带报告数据并持久记录消费。关键是数据、原 workflow 和处理确认一起交接，而不是在两条路径分别写历史、发空通知。即使模型可能自行轮询结果接口，也不能据此关闭可靠后台回传的缺口。

### N06. 资源清理仍有两条漏路，stop_confirmed 仍可能是假

第一条在 [executor.py:55](../nanobot/runs/executor.py#L55)：只要 cancel_session_work 正常返回就视为已清理。但 [loop.py:982](../nanobot/agent/loop.py#L982) 调用的子代理取消是有界等待，[subagent.py:423](../nanobot/agent/subagent.py#L423) 超时会保留仍活的任务，不会抛异常。

`runs/probe_cleanup_round2.py resistant` 通过真实 subagent 工具创建抗取消子任务，得到 `state=cancelled, stop_confirmed=true, effects_uncertain=false, isolation=false, live_owned_subagents=1`。下一 run 已 succeeded，原子代理仍活。`deadline` 也得到相同假停止，只是 outcome=deadline_exceeded。

第二条在 [executor.py:87](../nanobot/runs/executor.py#L87)：process_direct 抛异常时提前返回，没有执行后面的清理。`exception` 用受支持的 reraise 生命周期 hook 在真实 exec 子进程创建后抛错，得到 `failed/execution_error, stop_confirmed=true, effects_uncertain=false, isolation=false`，所属进程 returncode 仍为 null。

修复应由清理层返回类型化、可核查的资源存活结果，覆盖 task、subagent、exec 与清理任务，而非仅返回数量。所有退出分支必须经过收尾。未确认停止保留槽位和隔离，不能靠延长等待或把 task.cancel() 返回当作证明。两个反例都只创建了审核自己的等待任务和子进程，finally 已回收。

### N07. 隔离解除一次后，同实例再发生事故不能重新激活隔离

[store.py:463](src/nanobot_teams/coordinator/store.py#L463) 的 UPSERT 只更新 reason，不重置此前的 resolved_at。[mark_indeterminate:1028](src/nanobot_teams/coordinator/store.py#L1028) 也存在同类写法。

`coordinator/probe_quarantine.py` 先由真实 worker 观察不确定运行并隔离，模拟操作者确认，再观察第二个不确定运行。结果为 `first_isolated=true, second_effects_uncertain=true, second_stop_confirmed=false, second_isolated=false, third_task_admitted=true`。

修复应把每次事故作为新的隔离事件，或在重新激活时明确清除当前解决字段，同时保存此前确认审计。一次确认只处理当时事故，不能永久授权未来所有事故。

### N08. interrupted 的停止未确认，却释放额度并可被直接确认为取消

[store.py:507](src/nanobot_teams/coordinator/store.py#L507) 与 :537 仅按执行终态决定计数和预留，[request_cancel:893](src/nanobot_teams/coordinator/store.py#L893) 又把所有终态直接确认为取消。[worker.py:564](src/nanobot_teams/coordinator/worker.py#L564) 不再为终态驱动取消。

`probe_round2.py` 的 uncertainty 真实观察到 `interrupted, effects_uncertain=true, stop_confirmed=false`，同时 `principal_open=0, reserved_bytes=0`；之后取消返回 confirmed。没有新的停止证据支持该确认。

操作者确认风险后允许新工作，本身可以符合既定策略；这里的问题是未确认状态已经自动释放预留，且取消被冒充确证。修复应分离执行终态、停止确认、风险确认及费用结算。未知仍按原计量口径保留预留，后续取消不得以 terminal 代替实际停止。本结论不依赖把远端 interrupted 非法改成 succeeded 的模拟路径。

### N09. 正常 HTTP 注册的 Bot 没有 instance，迁移 unknown 仍可绕过

[http_api.py:504](src/nanobot_teams/coordinator/http_api.py#L504) 注册 Bot 时不绑定实例。[service.py:100](src/nanobot_teams/coordinator/service.py#L100) 的迁移映射仅对预绑定实例有效，否则 [service.py:151](src/nanobot_teams/coordinator/service.py#L151) 退回 legacy endpoint key。准入和 [worker.py:132](src/nanobot_teams/coordinator/worker.py#L132) 又只执行 instance 隔离。

`coordinator/probe_migration.py` 通过真实管理 HTTP 注册，按真实 0.4 Ledger 结构导入 unknown，再经真实 HTTP 确认新额度，得到 `registered_instance=null, unknown_isolated=1, admission_status=201, remote_submits=1`。实际提交后才绑定 inst_a。旧未知执行仍未核查，新运行已经发生。

这覆盖了 R5 migrate 使用的首次注册拓扑，而此前的回归直接给 Bot 填了 instance_id。修复应在允许执行前认证探测实例，并原子映射旧 endpoint 守卫；完成映射前 endpoint 隔离也必须有效。确认新额度不能解除 unknown。

### N10. 结果写入短暂失败，已经完成的成功结果被遗忘并改记取消

[service.py:387](../nanobot/runs/service.py#L387) 捕获结果提交故障后丢弃内存 ExecutionResult，finally 在 :395 删除运行跟踪。随后 [_reap_orphan_cancelling:283](../nanobot/runs/service.py#L283) 将其当作无执行器孤儿，改写为 cancelled。

`runs/probe_runs_round2.py result_persistence` 使用真实 SQLite query_only 制造结果写入失败，恢复写能力后得到 `state=cancelled, outcome=cancelled, text="", stop_confirmed=true, effects_uncertain=true, isolation=true, executor_calls=1`。最初 provider 的成功文本 PERSIST_ME 永久丢失；没有任何取消请求。风险隔离是保守的，但不能使这个取消分类变成正确。

修复应保留原始结果的提交义务，在存储恢复后重试同一份结果，不重新执行模型或工具。如果进程在落盘前真正消失，恢复可以按未知或 interrupted 处理，不能在仍有结果可重试时主动遗忘，再用取消填补记录。

## 5. 已复现的 P2

| ID | 位置与行为 | 本轮实际输出 | 修复方向 |
| --- | --- | --- | --- |
| M01 | [agent_runs.py:276](../nanobot/api/agent_runs.py#L276)、:286、:316 把任意异常映射不存在 | `http_storage` 对确实存在的 run 注入 SQLite 读取故障，status/result/cancel 均为非 retryable 的 404 run_not_found | 只把明确不存在映射 404；存储故障为结构化可恢复 5xx，防止 Coordinator 误判远端记录丢失 |
| M02 | [store.py:659](../nanobot/runs/store.py#L659) 的事件清理没有未确认守卫 | `retention` 为 row_kept=true、effects_uncertain=true、events_before=3、events_after=0 | 未确认运行的行、正文及调查事件一起保留，游标推进也必须与保留事件一致 |
| M03 | [CoordinatorStore:624](src/nanobot_teams/coordinator/store.py#L624) 只检查导入次数，不检查字节 | `migration_bytes` 为 imported_result_used=32768、confirmed_result_limit=32768、new_task_admitted=true | 旧累计结果字节、当前最大预留及实际结算共同执行限制，不能仅存配置供展示 |
| M04 | [async_mcp.py:120](src/nanobot_teams/async_mcp.py#L120) 仍 eager request + response.json | `mcp_bounds` 接受 gzip，decoded_bytes=1048576 | 将已修 BotRunClient 的读取契约落实到 MCP 控制客户端；本轮只对该客户端证明超限，不宣称审遍所有客户端 |
| M05 | [config.py:16](src/nanobot_teams/coordinator/config.py#L16) 和 :103 不接收或传递新 scaffold 字段 | `probe_scaffold_config.py` 指定 preset 得 unknown config keys；省略后 loaded_preset=""，真实 scaffold 为 422 preset_required | 正式 loader 接收并校验 scaffold_model_preset、scaffold_user_config；从 coordinator.json 启动到 HTTP 生成的完整路径测试，不能只直接构造 dataclass |
| M06 | [agent_inbox.py:170](../nanobot/api/agent_inbox.py#L170) 的 revision 解析在错误边界外 | `malformed_revision` 对 terminal_revision="bad" 返回 text/plain HTTP 500 | 将字段解析放在结构化校验边界内，返回 400 invalid_field。此问题此前已存在，本轮首次单独证明 |
| M07 | [TeamsSettings.tsx:96](../webui/src/components/teams/TeamsSettings.tsx#L96) 的草稿身份只含 input 与 bot | React 真实组件丢响应后切换项目，输出 projects=[proj,second]、distinctKeys=1；新契约断言失败 | 将 project 纳入完整草稿语义，或先解决原提交再允许切换；保持重试的原正文与 key 一致，避免后端不同正文 409 |

## 6. R5 与运维交付的剩余缺口

新 `live_workbench_r5.py` 确实构造了 Planner gateway、MCP、两个 Bot 与 Coordinator，提供的 chain JSON 记录了 Writer 结果包含 Research marker。本轮认可其正常路径证据的进步，没有重复上一轮“根本没启动 Planner”的指控。但本轮未重新付费运行 GLM，因此这些 JSON 始终标为提交者证据，不能扩大为本轮对所有并发情形的验证。

R5 migrate 的证据记录 `unknown_isolated=1` 后仅确认新额度便接受 201，正好没有断言旧 unknown 仍阻断。N09 对同拓扑进一步证明实际提交发生。R5 restart 杀的是 Coordinator，不是 Planner 在 wake publish 与 dequeue 之间的故障点；它不能关闭 N02。长程常见的快任务回传到活跃 Planner，也没有被该链路用同步点覆盖，见 N05。

另有两项交付缺口，不混入上述 17 组反例计数：

- RunStore 已能持久隔离，但生产代码只有 [clear_isolation 定义](../nanobot/runs/store.py#L254)，调用方仍是测试。没有受支持的本地操作者恢复入口来记录 actor、reason、原事故和剩余风险。直接写 Python 或 SQLite 不是文档承诺的 CLI。Coordinator resolve_quarantine 也需接入实际操作者入口，且先修 N07。
- 工作台仍只有单 Task 取消，没有架构要求的“停止协作”，先撤销 workflow 再取消其未结任务的入口。当前实际 `_webui_mutation_path('teams.stop_workflow', ...)` 返回 404 unknown WebUI mutation action；现有 Teams mutation 注册和界面没有对应操作。本轮不把单个 Bot 的 cancel 通过算作全 workflow 停止通过。

## 7. 下一轮修复单元

| 顺序 | 单元 | 必须结束的反例 |
| --- | --- | --- |
| T1 | 执行所有权与结果提交 | N01、N06、N10。发送前留事实，所有退出路径做类型化资源确认，结果落盘重试不得改写执行结果 |
| T2 | 持久 continuation 与输入准入 | N02–N05。义务经持久消费确认才结束，绑定原 generation/grant，真实用户输入的普通和注入入口一致，模型确实收到报告 |
| T3 | 事故、未确认计量与迁移 | N07–N09、M02、M03。新事故能重新隔离，未知不自动退回预留，未绑定实例不能绕过迁移守卫，提供安全操作者入口 |
| T4 | API、配置和界面边界 | M01、M04–M07。完整真实 loader 路径，所有已声明的控制客户端读取契约，错误分类与完整草稿身份 |
| T5 | 全 workflow 停止及最终验收 | 补齐该用户入口，再验证正常链路与 T1–T4 的确定性交错，并核对最终代码、测试、安装包和进度 |

建议将发送、唤醒、停止确认和结果提交各自建成明确状态，而不是继续补散落的布尔条件。`attempts=0`、`task.done()`、`bus.publish 返回`、`run 为 terminal` 都不是下一层已经接收、停止或结算的充分证据。测试应在这些交接点设置同步屏障，证明状态和实际工作一致。

已有通过证据只有在相关改动影响其假设时重跑。不得用把反例删掉、延长 timeout、把不确定改 terminal、放松断言或重复正常 live 场景来关闭发现。T2 的重启还需遵守宿主恢复确认策略，但必须留有可恢复义务和用户可见状态。

## 8. 复跑与工件

工件在 [review/round2-20261109](review/round2-20261109/)。使用 `pwsh`，从仓库根运行。脚本保留本机目标仓库绝对路径；换机器时先修改并核对模块导入位置。主机上的原始临时路径也保存在各分项 REPORT 中。

```pwsh
./.venv-nanobot/Scripts/python.exe nanobot-teams/review/round2-20261109/coordinator/probe_round2.py
./.venv-nanobot/Scripts/python.exe nanobot-teams/review/round2-20261109/coordinator/probe_quarantine.py
./.venv-nanobot/Scripts/python.exe nanobot-teams/review/round2-20261109/coordinator/probe_migration.py
./.venv-nanobot/Scripts/python.exe nanobot-teams/review/round2-20261109/coordinator/probe_scaffold_config.py
./.venv-nanobot/Scripts/python.exe nanobot-teams/review/round2-20261109/inbox/probe.py crash_queued replaced_generation revoked_after_inject active_result injected_user_revocation happy_path closures malformed_revision
```

运行资源与存储反例：

```pwsh
./.venv-nanobot/Scripts/python.exe nanobot-teams/review/round2-20261109/runs/probe_cleanup_round2.py resistant
./.venv-nanobot/Scripts/python.exe nanobot-teams/review/round2-20261109/runs/probe_cleanup_round2.py deadline
./.venv-nanobot/Scripts/python.exe nanobot-teams/review/round2-20261109/runs/probe_cleanup_round2.py exception
./.venv-nanobot/Scripts/python.exe nanobot-teams/review/round2-20261109/runs/probe_runs_round2.py result_persistence
./.venv-nanobot/Scripts/python.exe nanobot-teams/review/round2-20261109/runs/probe_runs_round2.py http_storage
./.venv-nanobot/Scripts/python.exe nanobot-teams/review/round2-20261109/runs/probe_runs_round2.py retention
```

从 `webui` 运行 UI 工件，当前应为旧两项通过、项目维度一项失败：

```pwsh
bun x --no-install vitest run --config ../nanobot-teams/review/round2-20261109/ui/vitest.config.mts --reporter=verbose
```

Python 反例中的退出码 0 表示成功复现坏行为，不是产品通过。修复后需将观察转成正确契约断言。原始输出与细节见 [runs REPORT](review/round2-20261109/runs/REPORT.md)、[inbox REPORT](review/round2-20261109/inbox/REPORT.md)、[Coordinator REPORT](review/round2-20261109/coordinator/REPORT.md)。修复结论以实际值为准，不按日志标题判断。
