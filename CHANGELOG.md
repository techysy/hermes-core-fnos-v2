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

### 0.9.9.7 — 状态页终端改为原厂 TUI
- **改动**：状态页终端从 `hermes chat`（Python CLI）改为 `hermes --tui`（原厂 Node.js ink 界面），体验一致
- **依赖**：ui-tui + node（0.9.9.5 已就绪）
- **验证**：`hermes --tui` 通过 PtyBridge 能在 PTY 正常启动

### 0.9.9.8 — 修复 dashboard Chat 缺 tui_dist
- **问题**：dashboard Chat 仍不可用
- **根因**：Chat 的 `_find_bundled_tui()` 找 `hermes_cli/tui_dist/entry.js`（预构建 TUI bundle），wheel 缺此文件（之前补的 ui-tui 是源码目录，Chat 用的是 tui_dist）
- **修复**：prebuild.sh 把 `ui-tui/dist/entry.js` 复制为 `hermes_cli/tui_dist/entry.js`

### 0.9.9.9 — dashboard 绑定 loopback 免认证（配合空壳反向代理）
- **改动**：dashboard 从 `--host 0.0.0.0` 改为 `--host 127.0.0.1`
- **原因**：v0.20.0 废弃 `--insecure`，绑定 0.0.0.0 强制认证；绑定 127.0.0.1（loopback）则免认证（auth gate 只在非 loopback 触发）
- **配合**：空壳 app（HermesDashboard）新增反向代理 `0.0.0.0:9118 → 127.0.0.1:9119`，局域网免登录访问 dashboard
- **验证**：dashboard 绑 127.0.0.1 首页 HTTP 200 无重定向（免登录）；反向代理转发正常
- **⚠️ 已回滚**（提交 a6b45f3）：改为继续绑定 `0.0.0.0` + 登录 `admin`（放弃 loopback 免认证方案，见 0.9.9.10）

### 0.9.9.10 — 修复状态页终端白屏 + dashboard Files 404
- **状态页终端白屏（根因：缺 xterm.js vendor 资源）**
  - **现象**：状态页「终端」面板一片空白
  - **根因**：`status_server.py` 引用了 `/vendor/xterm.min.js`、`xterm.min.css`、`xterm-addon-fit.min.js`，但 `cmd/vendor/` 目录**从 v1 迁移到 v2 时丢失**，fpk 里没有 → xterm.js 加载 404（实测 `GET /vendor/xterm.min.js` 返回 HTTP 404）→ `typeof Terminal === 'undefined'` → 终端面板不初始化，白屏
  - **修复**：从 v1（hermes-core-fnos）补齐 `cmd/vendor/{xterm.min.js, xterm.min.css, xterm-addon-fit.min.js}` 进 v2 仓库，随 fpk 打包
- **dashboard Files 页 404 `Path not found`**
  - **现象**：dashboard 的 Files 页面报 `Error: 404: {"detail":"Path not found"}`
  - **根因**：HermesCore 服务以 `HermesCore` 用户运行，其 `$HOME=/home/HermesCore` **不存在**；`/api/files` 默认把 managed-files 根解析到 `Path.home()` → 目录不存在 → 404
  - **修复**：cmd/main 启动 dashboard 时设置 `HERMES_DASHBOARD_FILES_ROOT=${HERMES_HOME}`，强制 managed-files 根指向存在的目录（web_server 会自动创建），Files 页可正常浏览
- **README**：补充徽章（Release/Downloads/fnOS/Hermes Agent/Upstream），并修正过时的 dashboard 绑定文档（0.0.0.0 + admin 登录）

### 0.9.9.12 — 状态页终端改为容器内 shell（废弃 hermes --tui）
- **改动**：状态页「终端」从 `hermes --tui`（原厂 Node.js ink 界面，体验差）改为**容器内 shell**（bash，回退 sh），PTY 直连
- **收益**：不再依赖 hermes/node/ui-tui 那套 TUI 启动链路；进入终端后可直接敲 `hermes model` / `hermes init` / `hermes chat` 等初始化命令
- **实现**：`_handle_pty_ws` spawn `bash`（`find_shell()` 回退 `sh`），cwd 指向 HERMES_HOME，并把 `HERMES_BIN` 所在目录加入 PATH 方便调用 hermes CLI
- **保留**：dashboard Chat 仍走 `hermes --tui`（ui-tui/tui_dist/node 依赖不动），状态页不再重复这一套

### 0.9.9.13 — 修复 Feishu 启动失败 `Permission denied: '/home/HermesCore'`
- **问题**：dashboard 消息平台卡片报 `Feishu / Lark: Feishu startup failed: [Errno 13] Permission denied: '/home/HermesCore'`，飞书连不上
- **根因**：HermesCore 服务以 `HermesCore` 用户运行，其 `$HOME=/home/HermesCore` **不存在**。Feishu adapter 的 app-lock（`gateway.status.acquire_scoped_lock`）默认把锁目录解析到 `$XDG_STATE_HOME` 或 `Path.home()/.local/state`（`Path.home()` 在 POSIX 上读 `$HOME` 环境变量）→ 落到不存在的 `$HOME` → `mkdir(parents=True)` 抛 `PermissionError` → 被 adapter `start()` 捕获为 `Feishu startup failed`
- **修复**：`cmd/main` 启动 gateway 前显式导出可写的 `HOME="${DATA_DIR}"`、`XDG_STATE_HOME="${DATA_DIR}/state"`、`HERMES_GATEWAY_LOCK_DIR="${XDG_STATE_HOME}/hermes/gateway-locks"`，使基于 home/XDG 的写入（含 Feishu 网关锁）全部落到 `DATA_DIR`（可写），与 0.9.9.10 的 `HERMES_DASHBOARD_FILES_ROOT` 修复同思路
- **验证**：重启内核后 Feishu 网关锁写入可写目录，adapter 不再抛 PermissionError

### 0.9.9.15 — 状态页新增代理配置 + HermesCore 代理出口
- **需求**：给 HermesCore 配置 HTTP 代理出口，使 Hermes 及其子进程（插件安装、pip、外网 API 调用）走 mihomo 代理。
- **实现**：
  - `cmd/status_server.py`：`CONFIG_FIELDS` 新增 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`（新分组 `proxy`「🌐 代理」），主配置面板渲染该分组，保存走既有 `/api/config` 白名单机制。
  - `cmd/main`：启动 gateway/dashboard 前 export `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`（含小写变体）。默认值 `http://127.0.0.1:7890`（本机 mihomo）；`NO_PROXY` 默认覆盖全部本机/内网段，避免 9Router(:20128)、mihomo(:7890)、飞书/微信等本地连接被误走代理。
- **说明**：可在状态页「基础配置 → 🌐 代理」分组改代理，保存后点重启生效；留空则回退默认本机 mihomo。
- **验证**：升级后 `core.log` 打印 `proxy: HTTP_PROXY=... NO_PROXY=...`；状态页出现代理分组。

### 0.9.9.16 — 修复升级覆盖 gateway.env 消息平台凭据
- **问题**：升级 fpk（0.9.9.15）后 `gateway.env` 里已配置的飞书凭据（FEISHU_APP_ID/SECRET 等）丢失，飞书连不上。
- **根因**：`install_callback` 的「更新场景」分支用 `cat > gateway.env` **整文件重写**，只含向导的 8 个字段，把已有的消息平台凭据（FEISHU/WEIXIN/QQ/DINGTALK）和代理字段全部覆盖掉。
- **修复**：`install_callback` 更新场景改为**就地更新**——只更新向导涉及的核心字段（API_SERVER_*/LLM_*/对应 provider key/DASHBOARD_*），保留 gateway.env 其余所有已有字段（`_update_env_kv`：存在则 sed 替换，不存在则追加）。
- **验证**：模拟测试确认飞书/微信/代理凭据在向导更新后原样保留。

### 0.9.9.17 — 状态页保存配置不再覆盖 gateway.env 自定义字段
- **问题**：状态页 `/api/config` 保存时 `_save_config` 遍历 `CONFIG_FIELDS` 白名单重写整个 gateway.env，会丢弃 gateway.env 中所有不在白名单的自定义字段（如 `install_callback`/其他工具追加的 `EXTRA_ENV`、未来新增 key），与消息平台设置存在覆盖风险。
- **修复**：`cmd/status_server.py` 的 `_save_config` 改为——只更新/保留 `CONFIG_FIELDS` 白名单字段，同时**原样保留 gateway.env 中所有不在白名单的自定义行**（读取 `extra_lines` 并在重写后追加），绝不因状态页保存消息平台/代理/内核配置而丢失任何自定义字段。
- **验证**：模拟测试确认自定义字段（如 `EXTRA_CUSTOM_FIELD`）、飞书 secret、代理、Router key 在状态页保存后全部保留，提交字段正常更新。

### 0.9.9.18 — 状态页代理分组显示默认值提示
- **问题**：状态页「🌐 代理」分组输入框为空（无默认值提示），用户看不到默认走本机 mihomo `http://127.0.0.1:7890`。
- **修复**：`cmd/status_server.py` 的 `_render_group_fields` 对 `proxy` 分组字段加 placeholder 默认值提示（HTTP_PROXY/HTTPS_PROXY 默认 `http://127.0.0.1:7890`、NO_PROXY 默认 `localhost,127.0.0.1,192.168.*`），留空即用默认。
- **说明**：代理默认值本由 `cmd/main` 启动时导出；状态页输入框留空 = 用 cmd/main 默认，填写则覆盖。
- **验证**：模拟渲染确认三个代理字段都有默认值 placeholder。

### 0.9.9.19 — HTTP/HTTPS 代理自动跟随
- **问题**：HTTP 和 HTTPS 代理在一般场景下不分（同一 mihomo 7890 同时处理两种协议），但当前 HTTP_PROXY/HTTPS_PROXY 独立配置，用户只填 HTTP_PROXY 时 HTTPS_PROXY 会被清空，导致 HTTPS 请求不走代理。
- **修复**：
  - `cmd/main`：`export HTTPS_PROXY="${HTTPS_PROXY:-${HTTP_PROXY}}"`——HTTPS_PROXY 未单独设置时自动跟随 HTTP_PROXY。
  - `cmd/status_server.py`：状态页 HTTPS_PROXY 输入框 placeholder 改为「留空则跟随 HTTP 代理」，HTTP_PROXY/NO_PROXY 保留默认值提示。
- **验证**：模拟三种场景——默认跟随 7890、只填 HTTP 时 HTTPS 跟随、单独填 HTTPS 时保留独立值，全部通过。

### 0.9.9.20 — 修复升级清空 hermes_home 数据（根因）
- **问题**：每次升级 fpk 都会丢 session、飞书/微信插件、消息平台凭据、记忆等所有用户数据。
- **根因**：fnOS 升级流程会先执行 `uninstall_callback` 再 `install_callback`。旧版 `uninstall_callback` 里 `rm -rf "${DATA_DIR}/hermes_home"` 把 Hermes 全部用户数据目录删掉，install 后只剩基础空壳。
- **修复**：`uninstall_callback` 改为**只删 venv（代码，install 会重建）和 *.log/*.pid（临时），保留 `hermes_home`**（用户数据，升级不丢）。真卸载如需彻底清除，README 提供 `rm -rf /vol4/@appdata/HermesCore/hermes_home` 手动方法。
- **说明**：真正卸载会残留含飞书/微信凭据的用户数据，属可接受；README「卸载与数据清理」已提供手动完整清理方法。
- **验证**：升级后 hermes_home 不再被清空（会话/插件/配置保留）。

### 0.9.9.22 — 状态页保存配置后自动重启内核
- **问题**：状态页保存配置（主配置面板 / 消息平台卡片）后只 `location.reload()` 刷新页面，需手动点 🔄 重启才生效；且刷新后跳到默认视图（聊天页），体验差。
- **修复**：`cmd/status_server.py` 的 `saveConfig` 和 `saveMsgCard` 保存成功后调用 `restartCore()` **自动重启内核**生效（`restartCore` 会提示"重启中"并 5 秒后自动刷新），不再只是刷新页面。
- **验证**：保存飞书/代理等配置后自动触发重启，状态页短暂不可用后自动刷新。

### 0.9.9.23 — 修复飞书插件启用了又被 write_config 丢掉
- **问题**：飞书插件 `hermes-lark-streaming` 已安装、飞书渠道已连通（gateway 2 platform），但 dashboard 提示"插件未启用"——config.yaml 的 `plugins.enabled` 不含它。
- **根因**：`cmd/main` 每次启动 `write_config` 重新生成 config.yaml，只写 `plugins.enabled: [dashboard_auth/basic]`，把 `hermes-lark-streaming` 丢掉；且 `setup_plugins` 在插件目录已存在时直接 return，不执行 `hermes plugins enable`。
- **修复**：
  - `setup_plugins`：插件目录已存在时也执行 `hermes plugins enable hermes-lark-streaming`（幂等）。
  - `write_config`：启动时提取旧 config.yaml `plugins.enabled` 里非 `dashboard_auth/basic` 的插件（`OLD_PLUGINS`），重写时合并保留。
- **验证**：模拟测试确认 OLD_PLUGINS 提取与合并后 YAML 结构有效（`plugins.enabled` 同时含 dashboard_auth/basic 和 hermes-lark-streaming）。

### 0.9.9.24 — 保留 config.yaml 中 hermes_lark_streaming 插件配置
- **问题**：用户直接改 config.yaml 的 `hermes_lark_streaming.footer.fields`（加 context/tokens），重启后配置被 `write_config` 覆盖回插件默认值。
- **根因**：`cmd/main` 每次启动 `write_config` 重写整个 config.yaml，只保留 `platforms` 段和 `plugins.enabled` 的非 basic 项，`hermes_lark_streaming` 段（插件配置）不在保留列表，被覆盖回默认。
- **修复**：`cmd/main` 增加 `OLD_HERMES_LARK` 机制——启动时提取旧 config.yaml 的 `hermes_lark_streaming` 段，`write_config` 重写后追加保留。
- **说明**：hermes-lark-streaming 插件无独立配置文件，`_get_hermes_config_path()` 直接读主 `config.yaml` 的 `hermes_lark_streaming` 顶层段（footer.fields 等）。用户自定义的插件配置段现可持久保存。
- **验证**：模拟测试确认 `hermes_lark_streaming.footer.fields` 含 context/tokens 且顺序正确，重写后完整保留。

### 0.9.9.25 — 修复状态页 500 崩溃（saveConfig 花括号未转义）
- **问题**：状态页渲染报 500，`KeyError: '\n    showMsg(I18N'`，状态页打不开。
- **根因**：0.9.9.22 给 `saveConfig` 加自动重启时，JS 里的 `{` `}` 写成**单花括号**，而状态页 `PAGE` 是 Python `.format()` 模板——`{` 被当作占位符，导致 KeyError。`saveMsgCard` 当时用的是正确双花括号，`saveConfig` 漏了。
- **修复**：`cmd/status_server.py` 的 `saveConfig` 中 `if (r.ok) {` / `}` 改为 `{{` / `}}`。
- **验证**：模板花括号校验残留未转义花括号 = 0；状态页可正常渲染。
- **补充**：状态页 `saveDefaultModel` 保存 `LLM_MODEL` 到 gateway.env，`write_config` 据此生成 `model.default`——默认模型已通过 gateway.env 关联，不会被覆盖（勿直接改 config.yaml 的 model.default）。

---

## 待办 / TODO
- [x] dashboard 认证方案，在 101 重装验证（0.9.9.11 登录 admin 正常）
- [x] dashboard Chat / 状态页终端，在 101 重装验证（xterm vendor 已就位）
- [x] dashboard Files 404 修复，线上验证通过（0.9.9.11）
- [ ] 空壳 app 桌面图标最终确认指向 :9118（反向代理）
- [ ] 测试通过后 `--formal` 发布 1.0.0
