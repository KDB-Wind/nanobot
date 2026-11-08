# Nanobot Teams 长程执行目标

> 用法：把本文件全文作为 `/goal` 的输入。它描述目标、边界和停止条件，不替代设计文档。

## 0. 一句话目标

在现有 MVP 基础上，按 `ROADMAP.md` 的 S0 → S6 连续执行到完成：多 Bot 定义与授权图、
多跳委派、生命周期工具、任务账本、发布准备，并保持对 Nanobot 核心零改动。

## 1. 执行模型与额度

- 你的编码模型是 **GLM-5.3**。整个任务期间不要切换编码模型。
- `step-5-preview` 和 `glm-5.3-flash` 都只出现在 **nanobot 运行时配置**里，是被测 bot
  使用的 preset（前者优先，后者回退），与你的编码模型无关。看到这两个名字不要把
  自己切换过去，也不要在 zcode 里改模型设置。
- 预算充足，但预算不是扩大范围的理由：
  - 不为了省 token 跳过测试或真实验收；
  - 也不为了消耗预算增加未被要求的抽象、UI 或依赖。

## 2. 工作区与已知事实

- 工作树：`C:/Users/KDB/orca/workspaces/nanobot/nanobot-teams-mvp`
- 分支：`feat/nanobot-teams-mvp`（已有多个本地提交，未 push）
- 代码目录：`nanobot-teams/`，只改这里
- 已有：88 个测试通过；服务端 6 个验收场景；WebUI 验收脚本
- 测试环境：`nanobot-teams/.venv`（插件）、`.venv-nanobot`（基线 Nanobot）
- 用户配置：`~/.nanobot/config.json`，`providers.stepfun.apiBase` 已指向
  `https://api.stepfun.com/step_plan/v1`，默认 preset 为 `step-5-preview`，
  回退为 `glm-5.3-flash`
- 提交策略：只做本地提交；不 push、不打 tag、不发布、不改 Nanobot 核心

## 3. 必读顺序

1. `D:/Code_Study/nanobot/ARCHITECTURE.md`
2. `D:/Code_Study/nanobot/IMPLEMENTATION_PLAN.md`
3. `D:/Code_Study/nanobot/RESEARCH_NOTES.md`
4. `nanobot-teams/ROADMAP.md`
5. `nanobot-teams/README.md`
6. `nanobot-teams/EXECUTION_LOG.md`（若已存在，说明是续跑，先读它）

四份文档的分工：前三份定义 MVP 与安全约束；`ROADMAP.md` 定义 MVP 之后的功能范围。
冲突时，安全与“不改核心”以三份设计文档为准，功能范围以 `ROADMAP.md` 为准。

## 4. 执行方式

- 按 S0 → S6 顺序推进，**不要在每个阶段停下来等待确认**。阶段门禁是自检：
  测试、验收、文档都通过就继续下一阶段。
- 一次只推进一个阶段；阶段内按可验收单元小步提交，每个单元都要能单独回滚。
- 每个单元完成后依次执行：
  1. 实现；
  2. 单元测试；
  3. 真实进程验收（不能用 mock 代替）；
  4. 更新 README 或 acceptance 文档；
  5. `git add` + 本地提交。
- 每完成一个阶段，更新 `EXECUTION_LOG.md`：阶段、状态、改动文件、验收命令与输出摘要、
  已知限制、下一步。
- 无人在场时自己做决定：在满足约束的前提下选最简单、最保守的方案，并把取舍写进日志。

## 5. 停止条件

只有以下情况可以结束本次运行：

1. S0–S6 全部完成，并通过最终验收。
2. 继续推进必须修改 Nanobot 核心、使用私有接口或破坏公开契约：
   记录到 `BLOCKERS.md`，跳到不依赖它的工作；没有剩余工作则结束。
3. 需要用户提供新凭据、资金、发布授权或不可逆操作：记录并跳过，继续其它项。
4. 同一问题用三种不同修复仍失败：记录复现步骤与证据，继续其它项。
5. 机器或环境故障导致无法继续，且没有可替代的独立工作可做。

没有 Unix 环境不是停止理由：先检查 WSL；仍不可用就把这一项写进 `BLOCKERS.md`，
继续 S1 及之后的工作。

除此之外不要停：不要问“是否继续”，不要等待确认，不要因为阶段完成而中止。

## 6. 质量底线

- 真实进程验收：两个独立 Nanobot 实例、公开 HTTP、MCP 工具调用；mock 只做补充。
- 不伪造上游没有的能力：没有远端状态、取消、幂等，就只记录本地观察。
- 密钥只从环境变量或配置读取；日志、提交、生成的配置不得包含明文密钥。
  验收生成的 `config.json` 用完必须删除或脱敏。
- 每次委派都要有超时、响应大小上限和脱敏。
- 只结束你自己启动的进程；不要杀未知进程，不要删除用户文件。
  清理范围限于本次生成的 `.e2e/` 临时文件和含密钥的临时配置。
- 每个阶段结束检查 `git diff --stat`，确认改动只在 `nanobot-teams/`。
- 最终必须 `pytest` 全绿、`ruff` 无告警。

## 7. 常用命令

```bash
cd C:/Users/KDB/orca/workspaces/nanobot/nanobot-teams-mvp/nanobot-teams

.venv/Scripts/python.exe -m pytest -q
ruff check src tests acceptance

NANOBOT_TEAMS_PRESET=step-5-preview .venv/Scripts/python.exe acceptance/run_e2e.py all
NANOBOT_TEAMS_PRESET=step-5-preview .venv/Scripts/python.exe acceptance/run_webui.py
```

如果 `ruff` 不在 PATH，用 `/c/software/Python311/Scripts/ruff`。

## 8. 最终交付

1. `EXECUTION_LOG.md`：S0–S6 状态表、每阶段验收命令与结果、已知限制。
2. `BLOCKERS.md`：未解决的问题、证据、可选方案（没有则写“无”）。
3. 最终验收：`pytest`、`ruff`、`run_e2e.py all`、`run_webui.py` 的实际输出。
4. 变更摘要：新增模块、配置字段、工具、CLI、数据库 schema。
5. `git log --oneline` 与干净的 `git status`。

## 9. 范围提醒

- 本路线中的“通信”是同步任务委派，不是自主群聊或主动推送。
- 不做：后台队列、跨进程幂等、远端取消、自动拉起 bot、exactly-once、任何核心改动。
- “在 UI 里新建 bot”目前不可实现；S3 交付的是 CLI 脚手架与校验工具。
- 预算很高不代表要做完 S6 之后继续加功能；S6 完成即停止并交付。
