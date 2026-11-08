# Nanobot Teams 执行日志(S0–S6 长程运行)

> 开始:2026-11-08 23:33(Asia/Shanghai)。执行者:ZCode(GLM-5.3),全程 poteto-mode。
> 决策链明细见 `decisions.tsv`;未解决问题见 `BLOCKERS.md`。
> 边界:只改 `nanobot-teams/`;Nanobot 核心零改动;只做本地提交。

## 状态总览

| 阶段 | 内容 | 状态 | 提交 |
| --- | --- | --- | --- |
| S0 | MVP 收尾:Unix 复跑 + 阶段 6 发布准备 | 完成 | `361d9997f` + 本阶段收尾提交 |
| S1 | 多 Bot Registry(单跳、多目标) | 未开始 | - |
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
