# Nanobot Teams 长程执行目标

> 用法：把本文件全文作为 `/goal` 的输入。它描述目标、方法、边界和结束条件，不替代设计文档。

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

## 2. 开发方法：全程使用 poteto-mode

**从第一个动作到最终报告，全程启用 `/poteto-mode`，不要中途退出。**

- 开始前启用 `/poteto-mode`。若当前环境不允许模型自行调用该 skill，就直接读取
  `C:/Users/KDB/.codebuddy/skills/poteto-mode/SKILL.md` 并严格按其规则执行，视为已启用。
- 上下文被压缩、会话恢复或换阶段时，重读一次 SKILL.md，恢复触发条件与原则，
  不要假设它仍然生效。
- 按触发条件调用该系列的 skills，不要机械地全部打开：
  - `how`：需要方案、架构取舍或"确定吗"的判断；
  - `architect`：代码跨函数边界，先并行探索设计再实现；
  - `swarm`：并行排查、覆盖矩阵、竞态与多假设验证；
  - `arena` / `interrogate`：设计有争议、需要对比或对抗审查；
  - `correct`、`no-comments`、`unslop`：收尾与文档；
  - `principle-*`：按场景引用，至少包括 prove-it-works、sequence-verifiable-units、
    boundary-discipline、model-the-domain、guard-the-context-window、
    never-block-on-the-human。
- 使用对应的 playbooks：`autonomous-run`（无人值守）、`feature`（功能实现）、
  `multi-phase-plan`（多阶段）、`shipping`（发布）、`session-pickup`（续跑）。
- 最终报告列出实际使用的 skills / principles，以及它们具体改变了哪个决定。
- `prove-it-works` 是本任务的验收标准：没有真实进程输出，就不能标记完成。

## 3. 工作区与已知事实

- 工作树：`C:/Users/KDB/orca/workspaces/nanobot/nanobot-teams-mvp`
- 分支：`feat/nanobot-teams-mvp`（已有多个本地提交，未 push）
- 代码目录：`nanobot-teams/`，只改这里
- 已有：88 个测试通过；服务端 6 个验收场景；WebUI 验收脚本
- 测试环境：`nanobot-teams/.venv`（插件）、`.venv-nanobot`（基线 Nanobot）
- 用户配置：`~/.nanobot/config.json`，`providers.stepfun.apiBase` 已指向
  `https://api.stepfun.com/step_plan/v1`，默认 preset 为 `step-5-preview`，
  回退为 `glm-5.3-flash`
- 提交策略：只做本地提交；不 push、不打 tag、不发布、不改 Nanobot 核心

## 4. 必读顺序

1. `D:/Code_Study/nanobot/ARCHITECTURE.md`
2. `D:/Code_Study/nanobot/IMPLEMENTATION_PLAN.md`
3. `D:/Code_Study/nanobot/RESEARCH_NOTES.md`
4. `nanobot-teams/ROADMAP.md`
5. `C:/Users/KDB/.codebuddy/skills/poteto-mode/SKILL.md`
6. `nanobot-teams/README.md`
7. `nanobot-teams/EXECUTION_LOG.md`（若已存在，说明是续跑，先读它）

四份文档的分工：前三份定义 MVP 与安全约束；`ROADMAP.md` 定义 MVP 之后的功能范围。
冲突时，安全与"不改核心"以三份设计文档为准，功能范围以 `ROADMAP.md` 为准。

## 5. 执行方式

- 按 S0 → S6 顺序连续推进，**不要在每个阶段停下来等待确认**。阶段门禁是自检：
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
- 整个运行期间不向用户提问。所有选择自行决定：在满足约束的前提下选最简单、
  最保守的方案，并把取舍写进日志。

## 6. 结束条件

**唯一的结束条件：S0–S6 全部交付，并通过最终验收。**

除"全部完成"之外，任何情况都不是结束理由：

- 缺凭据、缺授权、端口冲突、没有 Unix 环境：改用现有资源或降级方案完成其余部分，
  把该子项标记为"未验证"并写进 `BLOCKERS.md`，然后继续。
- 必须修改 Nanobot 核心才能继续：跳过该能力，保持核心零改动，记录限制，继续其它阶段。
- 同一问题三次不同修复仍失败：换方案或降级，继续；不要停。
- 需要破坏性操作：不执行，改用非破坏方案并记录，然后继续。
- 不要问"是否继续"，不要等待确认，不要因为某个阶段完成而中止。
- 只有全部阶段完成、日志与交付物齐全时，才输出最终报告并结束。

## 7. 质量底线

- 真实进程验收：两个独立 Nanobot 实例、公开 HTTP、MCP 工具调用；mock 只做补充。
- 不伪造上游没有的能力：没有远端状态、取消、幂等，就只记录本地观察。
- 密钥只从环境变量或配置读取；日志、提交、生成的配置不得包含明文密钥。
  验收生成的 `config.json` 用完必须删除或脱敏。
- 每次委派都要有超时、响应大小上限和脱敏。
- 只结束你自己启动的进程；不要杀未知进程，不要删除用户文件。
  清理范围限于本次生成的 `.e2e/` 临时文件和含密钥的临时配置。
- 每个阶段结束检查 `git diff --stat`，确认改动只在 `nanobot-teams/`。
- 最终必须 `pytest` 全绿、`ruff` 无告警。

## 8. 常用命令

```bash
cd C:/Users/KDB/orca/workspaces/nanobot/nanobot-teams-mvp/nanobot-teams

.venv/Scripts/python.exe -m pytest -q
ruff check src tests acceptance

NANOBOT_TEAMS_PRESET=step-5-preview .venv/Scripts/python.exe acceptance/run_e2e.py all
NANOBOT_TEAMS_PRESET=step-5-preview .venv/Scripts/python.exe acceptance/run_webui.py
```

如果 `ruff` 不在 PATH，用 `/c/software/Python311/Scripts/ruff`。

## 9. 最终交付

1. `EXECUTION_LOG.md`：S0–S6 状态表、每阶段验收命令与结果、已知限制。
2. `BLOCKERS.md`：未解决的问题、证据、可选方案（没有则写"无"）。
3. 最终验收：`pytest`、`ruff`、`run_e2e.py all`、`run_webui.py` 的实际输出。
4. 变更摘要：新增模块、配置字段、工具、CLI、数据库 schema。
5. `git log --oneline` 与干净的 `git status`。
6. poteto-mode 使用记录：实际调用的 skills / principles，以及它们改变的决定。

## 10. 范围提醒

- 本路线中的"通信"是同步任务委派，不是自主群聊或主动推送。
- 不做：后台队列、跨进程幂等、远端取消、自动拉起 bot、exactly-once、任何核心改动。
- "在 UI 里新建 bot"目前不可实现；S3 交付的是 CLI 脚手架与校验工具。
- 预算很高不代表要做完 S6 之后继续加功能；S6 完成即停止并交付。
