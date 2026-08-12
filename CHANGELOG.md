# CHANGELOG

## 0.9.9 (重构版起步)

> v2 全新骨架：内核升级到官方 Hermes Agent v0.20.0（GitHub 源码），前端预构建进 fpk，空壳 app 指向 :9119 WebUI 配置。

### 新增 / Added
- **内核升级到官方 v0.20.0** — 从 PyPI v0.19.0 切换为 GitHub v0.20.0 源码预构建，`HERMES_NIX_BUILD=1 pip install git+...@v2026.8.3`
- **前端预构建进 fpk** — 打包时预构建 web_dist 进 `venv.tar.gz`，dashboard 用 `--skip-build` 秒起，无需现场 npm
- **空壳 app 指向 :9119 WebUI** — 空壳 `hermes-dashboard-fnos` 桌面 iframe 指向 dashboard Web UI（配置入口）
- **预构建脚本 `scripts/prebuild.sh`** — python312 重建 venv + 复用/构建 web_dist，产出 `app/venv.tar.gz`
- **打包脚本 `scripts/build.sh`** — fnpack build + 版本号自动累加（测试版第 4 位 / `--formal` 正式版）

### 变更 / Changed
- 版本号体系：从 0.6.x 切换为 0.9.9.x（测试）→ 1.0.0（正式）
- 安装模式：在线 pip 装 PyPI v0.19.0 → 离线解压预构建 v0.20.0
- venv 构建：python3.11 → python312（cp312 C 扩展，适配 fnOS）
- manifest 声明 `install_dep_apps = python312:nodejs_v24`（fnOS 自动带依赖）

---

## 0.9.9.x 测试排障记录（2026-08-13）

> 在 fnOS 101 上从 0.9.9.1 迭代到 0.9.9.6，逐项解决 v0.20.0 预构建/安装/运行的坑。

### 0.9.9.1 — 首版打包 + 验证链路
- 在 101 上验证 python312 建 venv + 装 hermes v0.20.0 可行
- 复用本机 web_dist 进 fpk，dashboard `--skip-build` 能识别
- **问题**：`aiohttp` 缺失 → gateway api_server 起不来（见 0.9.9.2）

### 0.9.9.2 — 修复 gateway :8642 起不来
- **问题**：`WARNING gateway.run: API Server: aiohttp not installed` → :8642 健康检查超时
- **根因**：`aiohttp` 是 hermes 的可选依赖（不在核心依赖），gateway 的 api_server 需要它；离线 venv 没装
- **修复**：prebuild.sh 构建时补装 aiohttp；install_callback 离线解压后检测补装
- **同时修复**：build.sh 测试版版本累加正则 bug（`\\.` 双重转义导致四段版本误判为五段，`0.9.9.1` 错变成 `0.9.9.1.1`）

### 0.9.9.3 — 修复 dashboard 无法绑定 0.0.0.0
- **问题**：`Refusing to bind dashboard to 0.0.0.0 — no auth providers are registered`
- **根因**：v0.20.0 插件 opt-in，`dashboard_auth/basic` 必须列在 `plugins.enabled` 才会加载
- **修复**：cmd/main 配置 basic_auth 时同时写 `plugins.enabled: [dashboard_auth/basic]`
- **又发现**：wheel 构建缺 `plugins/*/plugin.yaml`（pyproject package-data 只含 gateway assets）→ 插件仍无法加载

### 0.9.9.4 — 修复 wheel 缺 bundled 插件资源
- **问题**：dashboard 仍报 `no auth providers registered`
- **根因**：wheel 不含 `plugins/*/plugin.yaml`，而 hermes 插件发现要求每个插件目录有 `plugin.yaml`
- **修复**：prebuild.sh 构建后从源码补全 `plugins/*/plugin.yaml`、locales、skills、optional-mcps、providers
- **又发现**：dashboard Chat 报 `TUI workspace missing`（缺 ui-tui）

### 0.9.9.5 — 修复 Chat/TUI 缺 ui-tui 和 node
- **问题**：dashboard Chat 不可用（`Chat unavailable: 1`）
- **根因**：Chat 通过 PTY 跑 `hermes --tui`，需要 `ui-tui`（wheel 缺）+ node 运行时
- **修复**：prebuild.sh 补全 ui-tui；manifest 声明 `nodejs_v24` 依赖；cmd/main 将 node 路径加入 PATH

### 0.9.9.6 — 修复状态页 8648 渲染崩溃
- **问题**：状态页报 `KeyError: ' setTimeout(sendTermSize, 50); '`
- **根因**：status_server.py 的 PAGE 模板 JS 花括号未转义（4 处漏 `{{`/`}}`），`.format()` 误判
- **修复**：转义 4 处 JS 单花括号，Python 验证渲染成功

---

## 待办 / TODO
- [ ] dashboard Chat 实际验证（ui-tui + node 已就绪，待用户确认）
- [ ] 空壳 app 桌面图标最终确认指向 :9119
- [ ] 测试通过后 `--formal` 发布 1.0.0
