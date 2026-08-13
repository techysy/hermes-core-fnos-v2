# Hermes Core for fnOS v2

Hermes Agent 自包含全能套件（fnOS 应用）。内核升级到官方 **Hermes Agent v0.20.0**，前端预构建进 fpk。

## 架构

```
fnOS
├── HermesCore v2（内核套件，有文件权限，包含所有服务）
│   ├── Gateway        :8642   ← hermes gateway run（v0.20.0）
│   ├── 状态页+终端    :8648   ← status_server.py（PTY 原生终端 + hermes --tui）
│   └── Dashboard UI   :9119   ← hermes dashboard --host 127.0.0.1（loopback 免认证）
│        └── 文件/配置修改 → 走 :9119 API → 内核(Python) 读写
└── HermesDashboard（空壳套件，无文件权限，反向代理）
    └── 桌面图标 → 反向代理 0.0.0.0:9118 → 本机 127.0.0.1:9119（免登录 WebUI）
```

- **空壳 = WebUI（:9118 反向代理）**，用来配置；内核 = 包含所有服务。
- **免认证方案**：v0.20.0 废弃 `--insecure`，绑定 0.0.0.0 强制认证；故 dashboard 绑定 `127.0.0.1`（loopback 免认证），空壳 app 提供反向代理 `0.0.0.0:9118 → 127.0.0.1:9119`，局域网设备经空壳访问 dashboard 免登录。
- 前端对配置/文件修改走 :9119 API → HermesCore 内核(Python) 读写，规避空壳无文件权限问题。
- 端口：Gateway :8642 / 状态页+终端 :8648 / Dashboard :9119（loopback）/ 空壳代理 :9118。

## 版本状态

| 版本 | 状态 |
|------|------|
| 0.9.9.x | **测试版**（进行中，见 docs/TESTLOG.md） |
| 1.0.0 | 正式版（测试通过后发布） |

## 版本规划
- 测试版：`0.9.9.x`（build.sh 自动累加第 4 位）
- 稳定后发布正式版：`1.0.0`（`--formal`）

## 构建流程

### 1. 预构建内核（构建机 31.31 或 fnOS 101，需 python3.12 + Node）
```bash
bash scripts/prebuild.sh    # 产出 app/venv.tar.gz（v0.20.0 venv + web_dist + 全部资源）
```
> 默认复用本机现成 web_dist（v0.20.0 前端）；无则从源码 npm build。
> 会从源码补全 wheel 缺失的资源（plugin.yaml、ui-tui、locales、skills 等）。

### 2. 打包 fpk（NAS，需 fnpack）
```bash
bash scripts/build.sh            # 测试版 0.9.9.x
bash scripts/build.sh --formal   # 正式版 1.0.0
```

### 3. 安装
在飞牛 NAS 应用中心手动安装 `HermesCore-*.fpk`（Web UI 手动安装是官方方式，CLI 已废弃）。

## 目录结构
```
├── manifest              # fnOS 清单（v0.9.9，声明 python312:nodejs_v24 依赖）
├── VERSION               # 0.9.9
├── ICON.PNG / ICON_256.PNG
├── app/
│   ├── ui/               # 桌面入口 config（内核指向 :8648）+ 图标
│   └── venv.tar.gz       # 预构建离线 venv（构建产物，不入库）
├── cmd/
│   ├── main              # 生命周期 start/stop/status
│   ├── install_callback  # 离线解压 v0.20.0 venv
│   └── status_server.py  # 状态页 + 原生终端
├── scripts/
│   ├── prebuild.sh       # 预构建 v0.20.0 venv + web_dist + 资源补全
│   └── build.sh          # fnpack build + 版本累加
├── docs/
│   └── TESTLOG.md        # 测试日志（遇到的问题与解决）
└── wizard/install        # 安装向导
```

## 依赖
- **python312**：fnOS 自动安装（venv base，cp312 C 扩展）
- **nodejs_v24**：TUI/Chat 需要（manifest 声明）

## 上游
- [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)（内核，v0.20.0）
- [techysy/hermes-core-fnos](https://github.com/techysy/hermes-core-fnos)（v1，历史版本，最小可用保留）
