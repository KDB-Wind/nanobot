# Nanobot Teams 执行日志(S0–S6 长程运行)

> 开始:2026-11-08 23:33(Asia/Shanghai)。执行者:ZCode(GLM-5.3),全程 poteto-mode。
> 决策链明细见 `decisions.tsv`;未解决问题见 `BLOCKERS.md`。
> 边界:只改 `nanobot-teams/`;Nanobot 核心零改动;只做本地提交。

## 状态总览

| 阶段 | 内容 | 状态 | 提交 |
| --- | --- | --- | --- |
| S0 | MVP 收尾:Unix 复跑 + 阶段 6 发布准备 | 完成 | `361d9997f`、`289a13659` |
| S1 | 多 Bot Registry(单跳、多目标) | 完成 | `ef5af2e28` |
| S2 | 多跳与回复路由 | 未开始 | - |
| S3 | Bot 生命周期工具 | 未开始 | - |
| S4 | 任务账本与可观测性 | 未开始 | - |
| S5 | 管理与策略 | 未开始 | - |
| S6 | 硬化与发布 | 未开始 | - |

## S0:MVP 收尾(2026-11-08 完成)

### 做了什么

1. Windows 基线:`pytest -q` 88 通过,`ruff check src tests acceptance` 无告警。
2. Unix 环境:WSL2 Ubuntu 22.04(内核 6.18.33)从主仓库克隆 `feat/nanobot-teams-mvp`,
   `uv venv --python 3.11` 建 `.venv-nanobot`(装 `.[api]`)与 `nanobot-teams/.venv`
   (装本包 + pytest),`~/.nanobot/config.json` 文件级拷贝(不回显)。
3. WSL NAT 无直连外网:写 `acceptance/relay.py`(纯字节转发,绑 WSL 网关
   `172.25.240.1:17897` → 宿主代理 `127.0.0.1:7897`),WSL 侧 `http_proxy` 指向中继,
   `no_proxy=127.0.0.1,localhost` 保护本机健康检查。不改用户代理软件配置。
4. `run_e2e.py` 按 `os.name` 选解释器路径(Windows `.venv/Scripts`、POSIX `.venv/bin`),
   其余启停逻辑本就跨平台。
5. 阶段 6 收尾:`test_packaging.py` 新增源码静态扫描(禁止文件系统写调用),
   README 增加支持矩阵,acceptance/README 增加 Unix 章节与实测记录。

### 验收记录

| 命令 | 平台 | 输出摘要 |
| --- | --- | --- |
| `.venv/Scripts/python.exe -m pytest -q` | Windows | `88 passed in 2.84s` |
| `ruff check src tests acceptance` | Windows | `All checks passed!` |
| `.venv/bin/python -m pytest -q` | Ubuntu/WSL2 | `88 passed in 2.31s` |
| `.venv/bin/python acceptance/run_e2e.py all` | Ubuntu/WSL2 | 6/6 PASS(research_direct、delegate_basic、session_persistence、long_timeout elapsed=38.2s、unknown_block、plugin_manifest) |

Windows 侧 6 场景 + WebUI 场景在 MVP 验收(commit `9c2e90082`)已通过;最终验收时统一复跑。

### 已知限制

- WSL 出网依赖宿主侧中继进程(运行期常驻,随会话停止);这是本机环境适配,不是产品依赖。
- WebUI 验收只在 Windows 跑过(需要 Edge);Unix 无浏览器场景。
- Unix 平台为 WSL2,非裸机/容器 Linux;信号与路径行为以 WSL2 为准。

### 下一步

S1:多 Bot Registry。bots[] 增加 `capabilities[]`、`allowedCallers[]`、`modelPreset`,
caller 身份改为每实例 `--caller` 参数指定,`list_bots` 按授权过滤;三 bot 验收。

## S1:多 Bot Registry(2026-11-09 完成)

### 做了什么

1. Registry schema v2:授权从"全局 caller + allowed_targets"改为每目标的
   `allowed_callers` 列表;bots 增加 `capabilities[]`(短 token,≤8 个)、
   `model_preset`(advisory,经 list_bots 暴露)。JSON 字段沿用 v1 的 snake_case
   风格(ROADMAP 里的 camelCase 是描述性命名)。
2. 实例身份:`--caller <bot-id>` 命令行参数(受信控制面,非模型输入)。v2 必填;
   v1 默认取 `caller_bot_id` 且 --caller 必须与之一致(防止歧义)。
3. v1 归一化:v1 文件继续可加载,allowed target 归一为 `allowed_callers=[caller]`,
   固定 caller 成为默认身份。两个 schema 归一到同一 TeamConfig 模型。
4. 图校验:allowed_callers 引用的 bot 必须存在、不得自引用、不得重复;
   有 allowed_callers 的 bot 必须带 endpoint+api_key_env,没有的禁止携带。
   密钥按 caller 可达目标解析(中间跳 bot 只拿它够得着的 key)。
5. 验收场景:`multi_bot`(三个真实实例,planner 同轮委派 research 与 writer,
   双 nonce 回传)、`unauthorized_pair`(真实 MCP 进程 --caller writer,
   list_bots 为空、delegate 被拒、stub HTTP 计 0 次命中)。

### 验收记录

| 命令 | 平台 | 输出摘要 |
| --- | --- | --- |
| `pytest -q` | Windows / Ubuntu | `105 passed`(两侧一致;新增 16 个 v2/授权测试) |
| `ruff check src tests acceptance` | Windows | `All checks passed!` |
| `run_e2e.py unauthorized_pair` | Windows / Ubuntu | PASS |
| `run_e2e.py multi_bot` | Windows / Ubuntu | PASS(三实例、两授权对、真实模型) |
| `run_e2e.py delegate_basic`(v1 回归) | Ubuntu | PASS(v1 registry 不带 --caller 仍工作) |

### 已知限制

- `model_preset` 只是元数据,不驱动目标实例的模型选择(实例配置仍由用户管理)。
- 授权图是静态的:授权变更需要改 registry 并重启相关 MCP 实例。
- 会话 ID 公式含 schema_version:v1 升 v2 后同一团队线程映射到新会话
  (旧会话历史留在原 Nanobot 实例中,不丢失)。

### 下一步

S2:多跳与回复路由。信封(task/parent/root id、hop、deadline_remaining)、
每跳预算扣减、环路检测、A→B→C 验收。
