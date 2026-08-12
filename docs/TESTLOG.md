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

## 核心经验 / 教训

1. **Hermes v0.20.0 wheel 缺大量资源**（plugin.yaml、web_dist、ui-tui、locales、skills 等）——官方设计是运行时通过 env-var 或源码布局提供。预构建 fpk 必须**从源码补全这些资源**。
2. **插件 opt-in**：`plugins.enabled` 必须显式声明，否则 dashboard auth 等不加载。
3. **aiohttp 是可选依赖**：gateway api_server 需要，需手动补装。
4. **TUI/Chat 依赖 ui-tui + node**：wheel 缺 ui-tui，node 需 manifest 声明 nodejs_v24。
5. **fnOS 上 venv 用 python312 建**（作为 base），不能直接搬 python311 venv。
