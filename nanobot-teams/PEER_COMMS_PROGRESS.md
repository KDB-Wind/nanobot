# Peer Comms 进度记录

目标是让已授权 Bot 异步派发任务、把结果送回发起方并继续会话，同时提供每 Bot 缓存统计。目标契约见 [PEER_COMMS_GOAL.md](PEER_COMMS_GOAL.md)。本文件只描述当前交付状态。历史过程保留在 Git 历史中。

## 当前基准

本轮从 PR #18 的 `197e832184e4af4d5961c17dc1a1eb9678fca61a` 开始修复。工作分支是 `fix/pr18-review-findings`。合并和发布仍由操作者决定。

原基准 CI run `43065576085` 的 16 个 job 全部成功。Linux 3.11 日志为 11,792 passed、86 skipped。这是原基准证据，不是本轮修改后的 CI。

## 本轮审核发现

| 项目 | 原因 | 当前状态 |
| --- | --- | --- |
| cron 独立运行被去重 | 手动运行和定时重投递都从 next_run_at_ms 推导身份 | 两项回归先红后绿，cron 217 passed |
| idle peer 会话不继续 | serve 启动 InboxWakePump，没有 AgentLoop.run 消费者 | 补齐 consumer 和共享 ExternalInbox，真实独立续跑通过 |
| 唤醒验收过宽 | 允许 persisted，通过原始 run 的结果推断后续轮次 | 真实模型验证唯一注入、wake_due=0、接受 turn id 和完成回复 |
| 回执崩溃恢复未覆盖 | 原场景没有 reply capability，并用 sleep 选择崩溃点 | 真实 inbox 提交后强杀协调器，重启后唯一性验证通过 |
| 浏览器检查数错误 | 把整体 PASS 汇总计为第八项 | 七项具名检查实跑全部通过，摘要与截图在外部目录 |
| 台账互相矛盾 | 当前总结与旧阶段表混在一起 | 本文件替代旧完成总结 |

## 验证入口

```text
python -m pytest tests/cron -q
python -m pytest tests/runs/test_serve_inbox.py tests/cli/test_commands.py -q
python -m pytest nanobot-teams/tests -q
python nanobot-teams/acceptance/peer_comms_probe.py --evidence-dir <外部证据目录>
python nanobot-teams/acceptance/peer_webui.py --output-dir <外部截图目录>
```

接受探针支持的运行路径参数见 `--help`。运行时必须使用本次工作树的源码，所有 Bot 使用隔离 home。模型配置只从既有操作者配置解析，凭据不进入验证摘要。原始日志、数据库和新截图保留在源码树之外。

## 交付状态

P0 至 P4 的身份、绑定、授权边及派发实现沿用原基准。P6 的统计公式和只读读取沿用原基准。P5 的真实唤醒和回执崩溃恢复因本轮发现重新验收。P7 的浏览器结果按实际具名断言计数。P8 本地修复与验收完成，远端 CI 和操作者合并仍分别记录。

修复已推送到 PR #18。2026-11-11 查询新提交没有 CI run，手动执行 `gh workflow run ci.yml --repo KDB-Wind/nanobot --ref feat/nanobot-teams-mvp` 返回 HTTP 422，原因是 `Actions has been disabled for this repository`。远端 CI 当前受仓库设置阻塞，不能引用原基准的全绿结果作为本轮验证。

原 wheel 的 SHA-256 为 `3a458d7b6b4c5a08f0a513c98aad889efb68b0c3c88e7d12d116d8d5d6484e59`，仅代表原基准插件产物。本轮没有据此声称宿主修改已经被打包发布。独立插件仓此前同步到 `c44b17390`，不代表本轮验收脚本已同步。

## 下一步

本轮组合回归 676 passed。完整 CLI 与 serve 套件单独运行 179 passed、1 skipped。websocket 同目录漏迁移的 callback 已补齐，原失败测试复跑 1 passed。频道投递与维护控制中的两处旧 callback 也已迁移，完整两套测试 58 passed。受影响生产模块 BasedPyright 为 0 errors、0 warnings，Ruff 与 223 文件导入边界检查通过。WebUI 生产构建通过，浏览器七项检查通过。`live_chain` 与 `crash_restart` 在真实四 Bot 和真实模型上分别通过，连同注册共三个场景通过。其余原基准场景未在本轮重复执行。独立代码审核发现的 callback 遗漏已修复。合并 `main` 和发布 PyPI 的授权状态见 [OPERATOR_GATE.md](OPERATOR_GATE.md)。
