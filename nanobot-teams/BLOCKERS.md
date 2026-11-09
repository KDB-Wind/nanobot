# Nanobot Teams 阻塞与未验证清单

> 记录无法在约束内解决的事项:现象、证据、可选方案、影响范围。
> GOAL.md 规则:不因此停机;用降级方案完成其余部分并继续。

## 无阻塞项(2026-11-09,S6 结束时)

未验证/未交付子项(均为记录,非阻塞):

1. ~~**CI workflow 未执行**~~ **已解决(2026-11-09)**:代码已推送到私有仓库
   `KDB-Wind/nanobot-teams`,`ci` 在 `ubuntu-latest` 与 `windows-latest` 上连续
   通过(`ruff` + `pytest`)。首跑暴露了未固定 ruff 版本造成的规则漂移:CI 装到
   0.17.4 后启用了新的默认规则集。已在 `pyproject.toml` 显式声明 lint 配置并把
   ruff 固定为 `0.17.4`。
2. **WebUI 验收未在 Unix 复跑**:需要 Edge 浏览器;Windows 已通过。
   影响范围:仅 WebUI 路径的可视化验收,服务端全场景双平台已覆盖。
3. **只读 Web 页面未交付**:ROADMAP S5 标注"可选";CLI graph/budget/audit
   覆盖同等数据。可选方案:后续独立静态页,不依赖官方接口。
4. **wheel 未发布**:构建与全新环境安装已验证;PyPI 上传与包名占用核验
   需用户授权,未执行。

环境备注(运行期):WSL NAT 客户机无直连外网,Unix 验收经宿主侧
`acceptance/relay.py` 字节中继出网;该中继随本次执行会话停止,不影响
产品代码与已记录的验收结果。