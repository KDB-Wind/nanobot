# Peer Comms 进度记录

目标契约：[PEER_COMMS_GOAL.md](PEER_COMMS_GOAL.md)（2026-11-10 由 D:\Code_Study\nanobot\TEAMS_PEER_COMMS_GOAL.md 复制入库）。本文件是执行真相源：阶段状态、命令、退出码、证据、剩余项、下一动作。上下文压缩后先读本文件与 `git diff`，不重做已 verified 且代码未变的工作。

## 基线

- 工作树：`C:\Users\KDB\orca\workspaces\nanobot\nanobot-teams-mvp`，分支 `feat/nanobot-teams-mvp`，HEAD `db8e9ba5685ffca9357cbe1f9aa49f478599226b`（PR #16 合并提交）。
- 未提交内容：0.4 同步模式修复（5 个 tracked 文件，680 行 diff）+ 未跟踪文档与 review 证据。已备份至 `C:\Users\KDB\orca\workspaces\nanobot\backups\peer-comms-p0-20261110\`（head.txt / status.txt / tracked.patch / untracked/ 156 文件 / untracked.sha256）。永不 stage/reset 这些内容。
- Python：`C:/Users/KDB/orca/workspaces/nanobot/nanobot-contribute/.venv/Scripts/python.exe`；PYTHONPATH 用分号：`<repo>;<repo>\nanobot-teams\src`。
- 宿主无 goal 工具（ZCode CLI 无 create_goal/update_goal）；objective 原文已存 `.audit/peer-comms-objective.txt`，本文件承担进度职责。

## 阶段表

| 阶段 | 状态 | 说明 |
| --- | --- | --- |
| P0 基线、审计与恢复点 | verified | 提交 36e099fd1（目标契约入库）+ f44122aff（基线/backlog）；备份与基线测试见 P0 记录 |
| P1 设计与威胁模型 | verified | 提交 00f611511；PEER_COMMS_DESIGN.md：领域对象、三方案对比（选定 A）、威胁模型、ADR-001；serve-inbox 探针 RED 实证（404 路由缺失 vs capabilities 宣告 inbox:true） |
| P2 失败探针（先红） | verified | 提交 0e700deee；acceptance/peer_comms_probe.py 六场景，证据 review/peer-comms/p2-probe-20261110-090521.jsonl：peer_dispatch 首断言 401 unauthorized，crash_restart blocked，全 RED exit 1 |
| P6 用量与缓存统计 | verified | ✓ a13b90033：coordinator/usage.py 只读读取器（mode=ro+query_only；observed 分母；缺失/未知 schema=unavailable+reason）+ /v1/teams/usage 与 /bots/{id}/usage 路由；test_peer_usage.py 10 绿（CACHE-01..05 fixture 全覆盖）；真实数据待探针 usage_stats 场景 |
| P7 工作台 | verified（浏览器验收待 P8 窗口） | ✓ 4a530059f：用量面板（null=未报告非 0%、分母说明）、Bot 关系图、peer 路由列，中英文 i18n；vitest 17 绿 + typecheck 0 错 |
| P5 回传、唤醒与崩溃恢复 | verified | **终版探针 9/9 GREEN exit 0**（p8-final-green.jsonl）：live_chain 真实模型全链 PASS（send_to_bot→writer 真跑→capability→发起方 inbox validate 200→投递 202→receipt injected 唤醒→research 汇总带 marker）；crash_restart 强杀收敛 PASS |
| P8 交付与独立审核 | in_progress | 待：文档/版本 0.6.0/wheel/全矩阵/独立审核/PR/浏览器截图 | Unit D+E ✓ 1277a7465 + a13b90033（durable peer 会话）+ 4a530059f（PeerConversationBinding 直达 mint——turn 自建 ctx 覆盖环境绑定，改专用 contextvar；serve 挂 usage 观察者）。探针 7/9 场景 PASS（含 crash_restart 真实强杀收敛）；live_chain 剩 capability 流（本轮修复复跑中）+ usage_stats 剩真实库（观察者本轮生效）|
| P4 授权图、派发与环路保护 | verified | UnitA ✓ 8b27a7e56；UnitB ✓ ad5348128；UnitC ✓ 843e80daf。探针 p2-probe-20261110-100324：peer_dispatch P4 门绿（真实 glm run succeeded+result_available），unauthorized/hop_guard/idempotent/events 全 PASS。crash_restart 的 403 已修（bot 作用域读取）待复跑 | UnitA ✓ 8b27a7e56+2645a3b1d（peer_edges+信封列+守卫：11 测试）；UnitB ✓ ad5348128（peer/send 路由+边管理+审计：9 测试）；UnitC lifecycle+MCP 工具（待提交）。探针证据 p2-probe-20261110-093113.jsonl：**真实模型 run succeeded + result_available=true**；unauthorized/hop_guard/idempotent_key/events_shape 四场景 PASS；peer_dispatch 剩 P5 回传门；crash_restart 剩探针 lookup 修复后复跑 |
| P3 身份、绑定与迁移 | verified | Unit1 ✓ 509047f39（ADR-001：单测 4 绿 + serve_inbox 探针真实进程 GREEN）；Unit2 ✓ d46f13e84（9 测试绿，Teams 289 passed，ruff 绿）；peer 探针进展 401→404（p2-probe-20261110-091946.jsonl：bot 凭据真实进程通过，缺 peer 路由= P4） |
| P2 失败探针（先红） | pending | |
| P3 身份、绑定与迁移 | pending | |
| P4 授权图、派发与环路保护 | pending | |
| P5 回传、唤醒与崩溃恢复 | pending | |
| P6 用量与缓存统计 | pending | |
| P7 工作台 | pending | |
| P8 交付与独立审核 | pending | |

## P0 记录

- 2026-11-10：读毕根 AGENTS.md、.agent/design|security|gotchas|navigation|workflow、ASYNC_WORKBENCH_ARCHITECTURE.md、ASYNC_WORKBENCH_IMPLEMENTATION_PLAN.md、REPAIR_STATUS.md、BLOCKERS.md、ROADMAP.md、PR16_REVIEW{,_D503A883,_ROUND4}.md、TEAMS_PEER_COMMS_GOAL.md 全文。
- 备份（退出码 0）：`backups/peer-comms-p0-20261110/`。tracked.patch SHA-256 `486e5b662d6861b04f4818eaa62a324cea04da053c0f1882516e4639235bc0a7`，680 行；untracked 156 文件 1.6M 带逐文件校验和。
- 基线测试（2026-11-10，两套均退出码 0，全绿）：
  - Teams：`python -m pytest nanobot-teams/tests -q` → **280 passed in 63.69s**（目标文档预期 281，差 1 来自 PR16 round-5 新提交的测试变动，实际为准）。
  - Nanobot 选中套件：`pytest tests/runs tests/session tests/tools/test_mcp_teams_meta.py tests/test_openai_api.py -q` → **373 passed, 3 skipped in 40.23s**（目标文档预期 371；工作树含未提交 test_openai_api.py 修复 + round-5 新增，实际为准）。
  - tests/agent 的 10 个 MCP fake session 失败为环境基线，本轮不修（目标文档 §3）。
- 首个提交：`36e099fd1 docs(teams): copy the peer comms goal contract into the repo`（nanobot-teams/PEER_COMMS_GOAL.md，SHA-256 445cbf0cd325... 与 D:\ 源逐字节一致）。
- 代码现状勘察：Explore 子代理运行中，结果待记。

## P1 记录

- 运行时探针（真实 `nanobot serve` 子进程）：`.audit/serve_inbox_probe.py`，证据 `review/peer-comms/p1-serve-inbox-probe.txt`，退出码 1（RED）。事实：serve 配置 `inboxEnabled:true` 后 capabilities 宣告 inbox:true 但 `POST /v1/agent-inbox` 404。结论：单进程收发需要 ADR-001 宿主改动（serve 挂 inbox），双进程共享 home 被架构禁止。
- 设计文档：PEER_COMMS_DESIGN.md（领域对象 7 个；方案对比选定 A，拒绝 B 代持/C 消息总线；威胁模型 7 项；`send_to_bot` 新工具而非改 submit_task 的理由；usage 读取器边界）。
- 代码勘察证据：Explore 子代理报告（2026-11-10），关键 file:line 已固化进 backlog 与设计文档。

## 剩余项 / 下一动作

- P2：写 `nanobot-teams/acceptance/peer_comms_probe.py` 四 Bot 拓扑失败探针（先红）。
