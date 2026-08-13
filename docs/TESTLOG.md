# HermesCore v2 测试日志 (TESTLOG)

> 记录 v2 (0.9.9.x) 在 fnOS 101 上的实际测试过程、问题与解决。
> 测试环境：fnOS 31.101 (Debian 12) / Hermes v0.20.0 / Python 3.12.4

---

## 测试进度总览

| 版本 | 测试项 | 结果 |
|------|--------|------|
| 0.9.9.1 | 预构建 venv + dashboard web_dist | ⚠️ gateway :8642 起不来 (aiohttp) |
| 0.9.9.2 | gateway :8642 + 版本累加 | ✅ gateway 起，dashboard 无法绑定 0.0.0.0 |
| 0.9.9.3 | dashboard 插件启用 | ⚠️ 插件仍不加载 (wheel 缺 plugin.yaml) |
| 0.9.9.4 | wheel bundled 资源 | ✅ dashboard 打开 (system 页)，Chat 不可用 |
| 0.9.9.5 | Chat/TUI (ui-tui + node) | ✅ dashboard 正常，待验证 Chat |
| 0.9.9.6 | 状态页 8648 渲染 | ✅ 花括号修复，渲染验证通过 |
| 0.9.9.10 | 状态页终端白屏 + dashboard Files 404 | ✅ 定位并修复（缺 xterm vendor + managed-files 根） |

---

## 详细测试记录

### 2026-08-13 00:59 — 0.9.9.1 首装

**操作**：在 fnOS 应用中心手动安装 `HermesCore-0.9.9.1.fpk`

**结果**：
- ✅ install_callback 离线解压 venv 成功，`Hermes Agent v0.20.0`
- ✅ venv 用 python312 重建，无路径依赖
- ❌ gateway 起不来：`WARNING gateway.run: API Server: aiohttp not installed`
- ❌ :8642 健康检查超时：`core start timeout on :8642`

**诊断**：
```
core.log: WARNING gateway.run: API Server: aiohttp not installed
         WARNING gateway.run: No adapter available for api_server
venv/bin/python -c "import aiohttp" → ModuleNotFoundError
```

**根因**：`aiohttp` 是 hermes 的 `[messaging]` 可选依赖，不在核心依赖；gateway 的 api_server 平台需要它。离线 venv 没装。

**修复**（0.9.9.2）：
- prebuild.sh 构建时补装 `aiohttp pyyaml cryptography`
- install_callback 离线解压后检测补装（联网兜底）
- 验证：补装后 `import aiohttp` → OK

---

### 2026-08-13 01:00 — 0.9.9.2 重装

**结果**：
- ✅ gateway :8642 起来（`core ready on :8642 after 2s`）
- ✅ 状态页 :8648 起来
- ❌ dashboard :9119 无法绑定 0.0.0.0

**诊断**：
```
dashboard.log: Refusing to bind dashboard to 0.0.0.0 — the auth gate
               engages on non-loopback binds, but no auth providers
               are registered.
```

**根因 1**：v0.20.0 插件 opt-in，`dashboard_auth/basic` 必须列在 `plugins.enabled` 才会加载。

**修复**（0.9.9.3）：cmd/main 配置 basic_auth 时写 `plugins.enabled: [dashboard_auth/basic]`。

**验证**：本机测试 `hermes dashboard --host 0.0.0.0`（带 plugins.enabled）→ 绑定成功。

**根因 2**（0.9.9.3 重装后仍失败）：wheel 缺 `plugins/*/plugin.yaml`。

```
hermes_cli/plugins.py: "Each directory plugin must contain a plugin.yaml manifest"
site-packages/plugins/dashboard_auth/basic/ → 只有 __init__.py，缺 plugin.yaml
```

**修复**（0.9.9.4）：prebuild.sh 构建后从源码补全 `plugins/*/plugin.yaml`、locales、skills、optional-mcps、providers。

---

### 2026-08-13 01:26 — 0.9.9.4 重装

**结果**：
- ✅ dashboard :9119 成功绑定，能打开（显示系统状态页）
- ✅ Hermes v0.20.0 / Python 3.12.4 确认
- ❌ dashboard Chat 不可用：`Chat unavailable: 1`

**诊断**：
```
dashboard.log: Error: the TUI workspace is missing from this Hermes checkout.
               Expected directory: .../site-packages/ui-tui
```

**根因**：Chat 通过 PTY 跑 `hermes --tui`，需要：
1. `ui-tui`（TUI workspace，wheel 缺）
2. node 运行时（TUI 是 Node.js 写的，101 无 node）

**修复**（0.9.9.5）：
- prebuild.sh 补全 `ui-tui`
- manifest 声明 `install_dep_apps = python312:nodejs_v24`
- cmd/main 将 `/var/apps/nodejs_v24/target/bin` 加入 PATH

---

### 2026-08-13 07:48 — 0.9.9.5 重装后访问状态页 8648

**结果**：
- ❌ 状态页 `192.168.31.101:8648` 连接被关闭 (ERR_CONNECTION_CLOSED)

**诊断**：
```
status.log: KeyError: ' setTimeout(sendTermSize, 50); '
            File "status_server.py", line 2070, in _render_page
            html = PAGE.format(...)
```

**根因**：status_server.py 的 PAGE 模板 JS 花括号未转义（第 1030、1430、1432、1437 行），`.format()` 把 JS 的 `{...}` 误判为占位符。

**修复**（0.9.9.6）：转义 4 处 JS 单花括号为 `{{`/`}}`。

**验证**：Python 用 string.Formatter 解析渲染 PAGE → 成功，输出 47KB，无 KeyError。

---

### 2026-08-13 07:58 — 0.9.9.6 重装后 dashboard Files/Chat

**结果**：
- ✅ dashboard :9119 打开（系统状态页正常，Hermes v0.20.0 / Python 3.12.4）
- ❌ dashboard **Files 404**：`Error: 404: {"detail":"Path not found"}`
- ❌ dashboard **Chat unavailable: 1**

**诊断**：
```
dashboard.log: HERMES_DASHBOARD_READY port=9119  (无 TUI 缺失错误, ui-tui 已补)
```

**Files 404 根因**（待确认）：dashboard 文件管理 API 404，可能是前端请求路径与 v0.20.0 API 不匹配，或 managed files 根目录问题（待排）。

**Chat 根因**（0.9.9.8 修复）：Chat 的 `_find_bundled_tui()` 找 `hermes_cli/tui_dist/entry.js`（预构建 TUI bundle），wheel 缺此文件（之前补的 ui-tui 是源码目录，Chat 用的是 tui_dist，不是 ui-tui）。

---

### 2026-08-13 08:00 — 0.9.9.8 补 tui_dist + 状态页终端 TUI

**结果**：
- ✅ 补 `hermes_cli/tui_dist/entry.js`（从 ui-tui/dist/entry.js 复制），`_find_bundled_tui` 能找到
- ✅ 状态页终端改为 `hermes --tui`（原厂 TUI，0.9.9.7 改动），通过 PtyBridge 能启动

**验证**：
```
hermes --tui (无 TTY) → "hermes-tui: no TTY" (普通 shell 会报)
hermes --tui via PtyBridge → spawn OK (PTY 环境正常)
```

---

### 2026-08-13 08:20 — 免认证方案（0.9.9.9 + 空壳反向代理）

**需求**：空壳面板打开免登录直接进 dashboard

**方案探索**：
- ❌ `--insecure`：v0.20.0 已废弃（NO-OP），绑定 0.0.0.0 强制认证
- ❌ URL token：gated 模式（绑 0.0.0.0）下 `?token=` 被拒绝
- ✅ **dashboard 绑 127.0.0.1（loopback）免认证** + 空壳反向代理

**验证**：
```
dashboard --host 127.0.0.1 → 首页 HTTP 200 无重定向 (免登录!)
反向代理 0.0.0.0:9118 → 127.0.0.1:9119 → 转发正常 (path 保留, Host 重写)
```

**实现**（HermesCore 0.9.9.9 + 空壳 1.0.0.1）：
- HermesCore：dashboard `--host 0.0.0.0` → `--host 127.0.0.1`
- 空壳 HermesDashboard：新增 `cmd/proxy.py` 反向代理（0.0.0.0:9118 → 127.0.0.1:9119），cmd/main 启动/停止，ui/config 指向 9118

> ⚠️ 0.9.9.9 方案已回滚（提交 a6b45f3）：dashboard 改回绑 `0.0.0.0` + 登录 `admin`，放弃 loopback 免认证。

---

### 2026-08-13 14:00 — 0.9.9.10 定位状态页终端白屏 + dashboard Files 404（101 线上排查）

**结果**：
- ❌ 状态页「终端」面板白屏（xterm.js 未加载）
- ❌ dashboard Files 页 404 `{"detail":"Path not found"}`

**诊断（线上 101 实测）**：
```bash
# ① 白屏：/vendor/xterm.min.js 404
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8648/vendor/xterm.min.js
# → 404  (status_server.py 从 cmd/vendor/ 读 xterm.js，但该目录在 fpk 里不存在)

# ② Files 404：HermesCore 用户 $HOME 不存在
grep HermesCore /etc/passwd
# → HermesCore:x:968:901::/home/HermesCore:/usr/sbin/nologin
ls -ld /home/HermesCore   # → No such file or directory
```

**根因**：
1. **白屏**：`status_server.py` 引用 `/vendor/xterm.{min.js,min.css,addon-fit.min.js}`，但 `cmd/vendor/` 目录**从 v1 迁移到 v2 时丢失** → xterm.js 加载 404 → `typeof Terminal === 'undefined'` → `initTerm()` 直接 return → 终端面板空白。
2. **Files 404**：`/api/files` 默认把 managed-files 根解析为 `Path.home()`（HermesCore 用户 `/home/HermesCore`），该目录**不存在** → `_canonical_path(..., require_exists=True)` 抛 404 `Path not found`。

**修复**（0.9.9.10）：
- 从 v1（hermes-core-fnos）补齐 `cmd/vendor/{xterm.min.js, xterm.min.css, xterm-addon-fit.min.js}`（共 ~288KB）进 v2 仓库，随 fpk 打包。
- cmd/main 启动 dashboard 时设置 `export HERMES_DASHBOARD_FILES_ROOT="${HERMES_HOME}"`，强制 managed-files 根指向存在的 `/vol4/@appdata/HermesCore/hermes_home`（web_server 的 `_ensure_managed_root` 会自动创建/确认）。

**验证（venv 内模拟 policy）**：
```python
import os, hermes_cli.web_server as ws
os.environ["HERMES_DASHBOARD_FILES_ROOT"]="/vol4/@appdata/HermesCore/hermes_home"
root = ws._ensure_managed_root(os.environ["HERMES_DASHBOARD_FILES_ROOT"])
print(root.is_dir())  # → True，/api/files 可解析
```

---

## 已修复问题汇总

| # | 问题 | 版本 | 类型 |
|---|------|------|------|
| 1 | aiohttp 缺失 → gateway :8642 起不来 | 0.9.9.2 | 依赖缺失 |
| 2 | build.sh 版本累加正则 bug（五段版本号） | 0.9.9.2 | 脚本 bug |
| 3 | dashboard_auth 插件未启用 → 无法绑定 0.0.0.0 | 0.9.9.3 | v0.20.0 机制 |
| 4 | wheel 缺 plugin.yaml → 插件不加载 | 0.9.9.4 | wheel 打包缺资源 |
| 5 | wheel 缺 ui-tui → Chat 不可用 | 0.9.9.5 | wheel 打包缺资源 |
| 6 | node 缺失 → Chat/TUI 不可用 | 0.9.9.5 | 依赖缺失 |
| 7 | status_server PAGE 花括号未转义 → 8648 崩溃 | 0.9.9.6 | 模板 bug |
| 8 | wheel 缺 tui_dist/entry.js → Chat 仍不可用 | 0.9.9.8 | wheel 打包缺资源 |
| 9 | dashboard 绑 0.0.0.0 强制认证 → 无法免登录 | 0.9.9.9 | v0.20.0 安全加固 |
| 10 | cmd/vendor/ 丢失 → 状态页终端白屏（xterm.js 404） | 0.9.9.10 | 迁移漏拷资源 |
| 11 | $HOME 不存在 → dashboard Files 404 `Path not found` | 0.9.9.10 | 服务用户无 home |

## 核心经验 / 教训

1. **Hermes v0.20.0 wheel 缺大量资源**（plugin.yaml、web_dist、ui-tui、**tui_dist**、locales、skills 等）——官方设计是运行时通过 env-var 或源码布局提供。预构建 fpk 必须**从源码补全这些资源**。
2. **插件 opt-in**：`plugins.enabled` 必须显式声明，否则 dashboard auth 等不加载。
3. **aiohttp 是可选依赖**：gateway api_server 需要，需手动补装。
4. **TUI/Chat 依赖 ui-tui + tui_dist + node**：wheel 缺 ui-tui 和 tui_dist/entry.js；node 需 manifest 声明 nodejs_v24。注意 **Chat 用 `hermes_cli/tui_dist/entry.js`**（不是 ui-tui 源码目录）。
5. **fnOS 上 venv 用 python312 建**（作为 base），不能直接搬 python311 venv。
6. **v0.20.0 安全加固**：废弃 `--insecure`，绑定 0.0.0.0 必须认证。**免登录方案 = dashboard 绑 127.0.0.1（loopback 免认证）+ 空壳反向代理**（局域网经代理访问）。
7. **迁移仓库务必带上 `cmd/vendor/`（xterm.js 等本地静态资源）**：v2 复用 v1 资源时漏拷 vendor 目录，导致状态页终端 xterm.js 404 白屏。status_server 的本地静态资源必须在 fpk 里。
8. **fnOS 服务应用以专用用户运行、其 `$HOME` 常不存在**：dashboard 等按 `Path.home()` 解析默认路径的 API（如 `/api/files`）会 404。用环境变量（如 `HERMES_DASHBOARD_FILES_ROOT`）显式指定存在的根目录。
