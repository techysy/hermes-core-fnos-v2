# HermesCore 本地构建打包流程

> Hermes Agent v0.20.1 自包含全能套件（fnOS 应用），正式版 v1.0.0

## 架构总览

```
构建机 31.31 (Arch Linux)
├── bash scripts/prebuild.sh
│   ├── git clone hermes-agent 源码 (含 .git) → venv/src/hermes-agent
│   ├── python3.11 建 venv + editable 安装源码
│   ├── 复用 web_dist (前端)
│   └── 产出 app/venv.tar.gz (182MB, 含源码 + .git + 依赖)
│
└── git push → GitHub (manifest/README/CHANGELOG 等)

打包机 101 fnOS (x86)
├── bash scripts/build.sh
│   ├── 读取 VERSION 自动累加版本号
│   ├── fnpack build → HermesCore-<version>.fpk
│   └── 交付到 /vol1/1000/fnOS App/fpk/HermesCore/
│
└── appcenter-cli install-fpk → 部署到 fnOS 设备

目标设备 (fnOS)
├── install_callback 解压 venv.tar.gz
│   ├── fix_venv_paths(): 修复 python 软链 → python311
│   ├── fix_venv_paths(): 重写 editable finder 路径
│   ├── git safe.directory + origin + chown
│   └── hermes v0.20.1 就绪
│
├── cmd/main start()
│   ├── export 代理 env (HTTP_PROXY/HTTPS_PROXY/NO_PROXY)
│   ├── export git 配置 (/usr/bin/git 优先 + 代理 + safe.directory)
│   └── hermes gateway run :8642
│
└── 容器终端 (status_server.py)
    └── hermes update --check → Git pull (走代理)
```

## 安装依赖

manifest 声明（fnOS 自动安装）：

```
install_dep_apps = python311:nodejs_v24:git
```

| 依赖 | 说明 | 来源 |
|------|------|------|
| python311 | venv 构建 + 运行时（cp311 C 扩展） | fnOS 应用中心 |
| nodejs_v24 | Dashboard Chat TUI 需要 | fnOS 应用中心 |
| git | 容器内 `hermes update` 需要（兜底，优先用系统 `/usr/bin/git`） | fnOS 应用中心 |

## 版本管理

### 版本号规则

- **VERSION 文件**：单一来源，存放于仓库根目录
- **测试版**：`0.9.9.x`（build.sh 自动累加第 4 位）
- **正式版**：手动设 VERSION 为 `1.0.0`，手动 fnpack build（build.sh 的 `--formal` 会升第 3 位）

### 关键文件

| 文件 | 内容 |
|------|------|
| `VERSION` | 版本号，build.sh 读取 |
| `manifest` | version + desc + changelog + install_dep_apps + platform |
| `CHANGELOG.md` | 版本历史记录 |

## 构建流程

### 1. 预构建（31.31 构建机）

```bash
bash scripts/prebuild.sh
```

**环境要求**：python3.11 + Node.js（用于 web_dist 构建）

**步骤**：

1. **获取源码**（第 1/5 步）
   - 优先复用 `~/.hermes/hermes-agent` 本地 checkout（含 .git，v0.20.1）
   - 用 `git clone --depth 1 file://...` 浅克隆（只取被跟踪的代码，排除 venv/node_modules）
   - 备用：从 GitHub 浅克隆（`git clone --depth 1 --branch main`）
   - 设 origin 为 GitHub（供容器内 hermes update 使用）

2. **创建 venv + editable 安装**（第 2/5 步）
   ```bash
   python3.11 -m venv $BUILD_DIR/venv
   pip install -e $SRC  # editable 安装 hermes-agent 源码
   ```
   - 源码 checkout 移入 `venv/src/hermes-agent`（含 .git）
   - 写 `.install_method = git`
   - 依赖补装（aiohttp、pyyaml、cryptography）

3. **准备前端 web_dist**（第 3/5 步）
   - 优先复用 `~/.hermes/hermes-agent/hermes_cli/web_dist`
   - 备用：npm build

4. **补全 bundled 资源**（第 4/5 步）
   - plugin.yaml、locales、skills、ui-tui、tui_dist

5. **打包 venv.tar.gz**（第 5/5 步）
   ```bash
   tar czf app/venv.tar.gz -C $BUILD_DIR venv
   ```
   - 含 `venv/src/hermes-agent/.git`（hermes update 可用）
   - 约 182MB

**产物**：`app/venv.tar.gz`

### 2. 打包 fpk（101 fnOS NAS）

```bash
bash scripts/build.sh            # 测试版（自动累加第 4 位）
BUILD_AUTO=1 bash scripts/build.sh   # 免确认
```

**步骤**：
1. 读取 VERSION，自动累加版本号
2. 检查 `app/venv.tar.gz` 存在
3. fnpack build → HermesCore-<version>.fpk
4. 交付到 `/vol1/1000/fnOS App/fpk/HermesCore/`
5. 旧包归档到 `old_fpk/HermesCore/`

**产物**：`HermesCore-<version>.fpk`（约 182MB）

### 3. 手动构建正式版（如 1.0.0）

```bash
# 1. 设版本
echo "1.0.0" > VERSION
sed -i 's/^version.*/version               = 1.0.0/' manifest

# 2. 手动 fnpack build
rm -f HermesCore.fpk
fnpack build -d .
mv HermesCore.fpk HermesCore-1.0.0.fpk

# 3. 交付
cp HermesCore-1.0.0.fpk "/vol1/1000/fnOS App/fpk/HermesCore/"
```

## 部署流程

### 安装

```bash
# 方式一：fnOS 应用中心手动安装
# 选择 HermesCore-1.0.0.fpk → 手动安装

# 方式二：CLI 安装（需 wizard env 文件）
appcenter-cli install-fpk -e /path/to/env /path/to/HermesCore-1.0.0.fpk
```

### install_callback 流程

1. 解压 `venv.tar.gz` 到 `$DATA_DIR/venv`
2. `fix_venv_paths()`：
   - 修复 `venv/bin/python` 软链 → fnOS 的 python311
   - 修复 bin/* 脚本 shebang → 指向修复后的 python
   - 修复 pyvenv.cfg（home、executable）
   - **重写 editable finder 路径**：打包机路径 → 部署路径
     - ⚠️ 必须用 `$VENV_DIR/lib/python3.11/site-packages`
     - 不能用 `$TARGET_PY`（python311 应用）算 site-packages
   - 写 `.install_method = git`
   - 配置 git safe.directory + origin（GitHub）
   - chown 源码目录给运行用户（HermesCore）

### 升级

- 卸载旧版 → 安装新版，走 install_callback
- 旧 venv 备份为 `venv.old`
- `hermes_home`、`gateway.env` 等数据保留

## 架构字段规范

fnOS manifest 架构声明用 **`platform`**（非 `arch`）：

```ini
# ✅ 正确（新规范）
platform = x86

# ❌ 过时（旧版）
arch = x86_64
```

| 应用 | 字段 |
|------|------|
| dsh | `platform = x86` |
| 9router | `platform = x86` |
| HermesCore (v1.0.0) | `platform = x86` |

## 核心特性

### 面板「🌐 代理」设置

- 配置分组：HTTP_PROXY / HTTPS_PROXY / NO_PROXY
- 存于 `gateway.env`
- cmd/main 启动时 export 给 hermes gateway + 容器终端
- 大小写都 export（兼容 urllib/requests/git）
- 默认 NO_PROXY 覆盖内网（localhost, 192.168.\*, 172.16-31.\*）

### 容器内 `hermes update`

- 内核为源码 git checkout（含 .git）+ editable 安装
- 容器终端直接 `hermes update` 拉取 GitHub 最新
- 每次 `hermes update` 更新到 main 分支最新（v0.20.1+）
- 不再需要每次重打 150MB fpk

### git 配置（cmd/main）

```bash
export PATH="/usr/bin:$PATH"                    # 优先系统完整版 git（含 remote-https）
git config --global http.proxy "${HTTP_PROXY}"  # 走 mihomo 代理
git config --global https.proxy "${HTTPS_PROXY}"
git config --global --add safe.directory "..."  # 避免 dubious ownership 报错
```

## 关键踩坑记录

### 1. editable finder 路径重写

**问题**：install_callback 用 `$TARGET_PY`（python311 应用）计算 SITE_PKG_DIR，得到的是 python311 应用的 site-packages，不是 HermesCore venv 的，导致 editable finder 路径重写没找到文件，部署后 hermes 启动报 PermissionError。

**修复**：直接用 `$VENV_DIR/lib/python3.11/site-packages`（venv 自己的路径）。

### 2. venv/bin/python 软链断裂

**问题**：打包时 venv/bin/python 软链指向构建机（31.31）的 uv python 绝对路径，部署到 fnOS 后路径不存在。

**修复**：install_callback 的 fix_venv_paths() 修复软链 → fnOS 的 python311。

### 3. git remote-https 缺失

**问题**：fnOS 的 git 应用（`/usr/local/bin/git`）缺 remote-https，hermes update 的 fetch 失败。

**修复**：cmd/main 把 `/usr/bin` 前置到 PATH（系统自带完整版 git，含 remote-https）。

### 4. git dubious ownership

**问题**：源码 checkout 的 owner 与运行用户不同，git 报安全错误。

**修复**：`git config --global --add safe.directory` + install_callback 的 chown。

### 5. .git 写权限

**问题**：HermesCore 用户无法写 `.git/FETCH_HEAD`，git fetch 失败。

**修复**：install_callback 的 chown -R 给运行用户。

### 6. cp311 C 扩展跨机器兼容

**问题**：31.31（Arch, glibc 2.4x）编译的 cp311 C 扩展能否在 101（Debian, glibc 2.36）运行？

**结论**：✅ 可以，所有 .so 的 glibc 需求 ≤ 2.17，101 的 glibc 2.36 兼容。

## 文件清单

```
├── manifest              # fnOS 清单（v1.0.0，python311:nodejs_v24:git）
├── VERSION               # 版本号
├── scripts/
│   ├── prebuild.sh       # 预构建（构建机）
│   └── build.sh          # 打包 fpk（NAS）
├── cmd/
│   ├── main              # 生命周期（start/stop/status）
│   ├── install_callback  # 安装时解压 + 修复路径
│   └── status_server.py  # 状态页 + 原生终端
├── app/
│   └── venv.tar.gz       # 预构建产物（不入库）
├── docs/
│   ├── TESTLOG.md        # 测试日志
│   └── build-pipeline.md # 本文档
└── wizard/install        # 安装向导
```