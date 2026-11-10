# Peer Comms Backlog

P0 审计产物。每项含 id、证据（file:line 或文档位置）、严重性、决定（do/defer/reject）、理由。行号以 HEAD `db8e9ba56` 加首个提交 `36e099fd1` 后的工作树为准（2026-11-10 勘察复核）。

## 本轮新增范围（TEAMS_PEER_COMMS_GOAL.md §3–§6）

| ID | 项 | 证据 | 严重性 | 决定 | 理由 |
| --- | --- | --- | --- | --- | --- |
| PC-01 | 协调器只有单个 `PlannerBinding`，角色仅 planner/operator 两个固定 principal | `coordinator/config.py:28-43`（单 `planner: PlannerBinding \| None`）、`http_api.py:199-202`（principal_for 折叠为两个固定串） | P1 | do（P3） | 多 Bot 互通的结构性阻断；目标 I1/I3 |
| PC-02 | 托管 Bot 配置写死 `mcpServers: {}` 与 `channels: {}`，worker 无派发工具、无接收路径 | `lifecycle.py:135-149`（runs 块 + 工具块）、`schema.py:395`（inbox_enabled 默认 False） | P1 | do（P3/P4） | worker 发起任务需要 async MCP 客户端配置；接收需要 inbox |
| PC-03 | `auto_resume` 的 `reply_capability` 铸造入口通用（按 `runs_cfg.principal_id`，通配 operation_key/bot_id/instance_id，绑 session+generation+workflow） | `gateway_runtime.py:153-182, 639-642`、`external_inbox.py:152-177`、`async_mcp.py:346-358` | P2 | do（P5 复用） | 铸造不硬编码 planner；worker 间回传可复用同机制，无需新协议 |
| PC-04 | 缓存命中率当前为主机全局 `cache_read_rate_30d`，无 per-Bot 维度 | `nanobot/llm_usage/store.py:510-522`（公式）、`store.py:194-216`（表无 bot 列） | P1 | do（P6） | 每 Bot 独立 home → 独立 `<home>/llm_usage.sqlite3`，按库读取即可，无需改表；公式语义已符合目标 §5（observed 分母），需加 per-model 拆分与 7d/30d/all 窗口 |
| PC-05 | 单 Bot 同时执行 runs 与接收 inbox 结果，今天无任何单进程路径 | `cli/commands.py:415-441`（serve 挂 runs，零处读 inbox）、`gateway_runtime.py:505, 630-670, 1164-1217`（inbox sidecar 仅在 gateway，无 RunService） | P0 | do（P1 探针 + P3 落地） | 目标 §3 指定的第一个必须用探针回答的运行时问题；双进程共享 home 会争会话库（架构文档 §3 禁止） |
| PC-06 | peer 授权边、hop/环路/扇出/并发上限、peer task 幂等全缺 | 协调器路由无 `send_to_bot`、tasks 表无 conversation root/hop 列（`coordinator/store.py:56`，勘察确认） | P0 | do（P4） | 目标 I3/I4/I6 |
| PC-07 | 结果回传只支持 planner 固定 inbox | `http_api.py:267-316`（auto_resume 校验绑定 planner inbox） | P0 | do（P5） | 目标 I5：结果只回发起方 |
| PC-08 | 工作台无 Bot 图、peer 消息列表、每 Bot 用量面板 | `webui/src/components/teams/TeamsSettings.tsx`（单一快照组件）、`teams-api.ts:3-69`（无 usage 类型） | P1 | do（P7） | 目标 §5 界面契约 |

## 0.5 工作台遗留项（目标 §3 后半）

| ID | 项 | 证据 | 严重性 | 决定 | 理由 |
| --- | --- | --- | --- | --- | --- |
| PC-09 | 未提交文档与证据仍在工作树（5 tracked + 未跟踪文档/review 目录） | `backups/peer-comms-p0-20261110/status.txt`（27 行清单）、tracked.patch 680 行 | P1 | do（P8 分批入库） | 0.4 修复与新文档需进版本库；逐项审阅归属后提交，永不 reset |
| PC-10 | wheel `dist/nanobot_teams-0.5.0-*.whl` 构建于本轮修复之前 | `dist/` 目录（0.4.0 与 0.5.0 并存）；0.6.0 待建 | P1 | do（P8 重建 0.6.0 并新 venv 安装） | 协议新增按 semver 升 0.6.0（目标 P8） |
| PC-11 | PR #17（preamble clock 测试修复）开放中 | `gh pr list`：#17 `fix/teams-preamble-clock` | P2 | do（P8 收口） | PR16 终审非阻断项一的对应修复；先查其评审状态再决定合并或重做 |
| PC-12 | 独立仓库 `KDB-Wind/nanobot-teams` 需再次 subtree 同步 | PR16_D503A883 复审核对过同步哈希（`e20687d9b2`），此后 mvp 分支又有 round-5 提交 | P2 | do（P8） | 目标 P8 同步独立仓库并确认双平台 CI |
| PC-13 | 双平台 CI 只覆盖插件仓（ubuntu+windows：安装/ruff/pytest），主仓集成路径无 PR 触发 | PR16_D503A883「CI 和独立仓库同步已核实」节 | P2 | do（P8，证据按实际可触发范围记录） | 主仓 workflow 的分支过滤为 main；如实记录覆盖范围，不虚报 |
| PC-14 | 文档漂移：ROADMAP.md:35 「尚缺多 bot 权限图…任务账本」已过时（账本/生命周期已在）；DESIGN.md 大量未提交改动 | ROADMAP.md:35、DESIGN.md diff（459 行变更） | P2 | do（P8 更新文档） | 目标 P8 文档与代码一致 |
| PC-15 | `tests/agent` 10 个 MCP fake session 失败 | 目标文档 §3 与记忆（环境基线） | P3 | reject（不修） | 明确的环境基线，与本轮无关 |

## ROADMAP.md / BLOCKERS.md 未完成与未验证项

| ID | 项 | 证据 | 严重性 | 决定 | 理由 |
| --- | --- | --- | --- | --- | --- |
| PC-16 | WebUI 验收未在 Unix 复跑（需 Edge） | BLOCKERS.md:19-20 | P2 | defer（P7 记录 Windows 证据 + Unix 缺口；若 P7 窗口内可装浏览器则补） | 服务端双平台由插件仓 CI 覆盖；浏览器验收平台缺口如实记录 |
| PC-17 | 只读 Web 页面未交付（S5 可选） | BLOCKERS.md:21-23 | P3 | reject | 工作台 UI（PC-08）覆盖同等数据，S5 标注可选 |
| PC-18 | wheel 未发布 PyPI | BLOCKERS.md:24-25 | P2 | do（P8 写 OPERATOR_GATE，不自动发布） | 发布需用户授权；P8 只构建+本地安装验证 |
| PC-19 | S6 硬化：认证链、链级硬费用预算、服务身份认证、多租户隔离未提供 | BLOCKERS.md:5 | P2 | defer（本轮边界即受信单用户；peer 凭据作用域按 I3 落地） | 目标 §2 非目标明确不做多租户；服务身份仍靠部署边界 |

## PR #16/#17 非阻断意见

| ID | 项 | 证据 | 严重性 | 决定 | 理由 |
| --- | --- | --- | --- | --- | --- |
| PC-20 | preamble round-trip 时间敏感断言（89.999 vs 90.0） | PR16_REVIEW_D503A883 非阻断事项一；PR #17 修复中 | P2 | do（P8 并入 PC-11） | 已有开放 PR，收口即可 |
| PC-21 | 「cancelled turn 保留 marker」表述需限定生命周期（显式 Stop 会清 pending boolean） | PR16_REVIEW_D503A883 非阻断事项二；round-5 提交 `8c8cf4e4a`/`dd2f77df6` 已改行为 | P3 | do（P8 文档措辞核对） | 行为已修，文档表述待核 |

## 汇总

- do：PC-01…PC-14、PC-18、PC-20、PC-21（映射到 P1–P8 阶段）
- defer：PC-16（P7 记录缺口）、PC-19（边界声明）
- reject：PC-15、PC-17
