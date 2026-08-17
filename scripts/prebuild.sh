#!/usr/bin/env bash
# prebuild.sh — 预构建 Hermes v0.20.0 内核 + 前端，产出 app/venv.tar.gz（离线安装包）
#
# 运行环境：构建机（31.31）或任意有 python3.12 + Node 的机器
#   bash scripts/prebuild.sh                 # 默认用 python3.12
#   PY=/usr/bin/python3.11 bash scripts/prebuild.sh   # 指定 python（仅验证用）
#
# 产物：app/venv.tar.gz（含 v0.20.0 venv + 预构建 web_dist）
# 之后在 NAS 上：bash scripts/build.sh  -> fnpack build（fpk 内含 venv.tar.gz）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="${ROOT}/app"
BUILD_DIR="${ROOT}/.build"
VENV_TAR="${APP_DIR}/venv.tar.gz"

# 内核版本（官方分支/tag）— v0.20.1 在 main 分支 (无独立 tag, 靠 hermes update git pull 升级)
# 默认 main (最新); 若要锁版本可改具体 tag (如 v2026.8.3 = v0.20.0)
HERMES_TAG="main"
HERMES_REPO="https://github.com/NousResearch/hermes-agent"
# 复用本机现成的 web_dist（若提供）
WEB_DIST_SRC="${WEB_DIST_SRC:-/home/yangyu/.hermes/hermes-agent/hermes_cli/web_dist}"

# Python 解释器（默认 python3.11，fnOS 用 python311 应用；兼容 31.31 的 v0.20.1 环境）
PY="${PY:-}"
if [ -z "$PY" ]; then
    for cand in python3.11 /vol4/@appcenter/python311/bin/python3.11 /usr/bin/python3.11; do
        [ -x "$cand" ] && PY="$cand" && break
    done
fi
[ -z "$PY" ] && { echo "❌ 未找到 python3.11。可 PY=/path/to/python3.11 指定。"; exit 1; }
echo "使用 Python: $PY"

mkdir -p "$BUILD_DIR" "$APP_DIR"

echo "=== 1/5 获取 Hermes 源码 git checkout (含 .git, 支持 hermes update) ==="
cd "$BUILD_DIR"
# 优先复用构建机上现成的 hermes-agent checkout (31.31 的 v0.20.1, 含 .git)
# 否则 git clone 完整 checkout (必须含 .git, 供 hermes update 用)
if [ ! -d "hermes-agent-src/.git" ]; then
    # 优先复用构建机上现成的 checkout (31.31 的 v0.20.1, 含 .git)
    # 用 git clone 浅克隆: 只取被 git 跟踪的代码(23M) + 浅 .git, 排除 venv/node_modules 等未跟踪大目录
    # 用 file:// 本地协议, 不依赖网络, 秒级完成
    if [ -d "$HOME/.hermes/hermes-agent/.git" ]; then
        echo "  从本地 checkout 浅克隆: $HOME/.hermes/hermes-agent (${HERMES_TAG})"
        rm -rf hermes-agent-src
        git clone --depth 1 --branch "${HERMES_TAG}" "file://$HOME/.hermes/hermes-agent" hermes-agent-src 2>&1 | tail -3 || true
    fi
    # 若复用失败, 从 GitHub clone (含 .git)
    if [ ! -d "hermes-agent-src/.git" ]; then
        echo "  git clone hermes-agent from GitHub (${HERMES_TAG})..."
        rm -rf hermes-agent-src
        if [ -n "${GIT_PROXY:-}" ]; then
            git -c http.proxy="$GIT_PROXY" clone --depth 1 --branch "${HERMES_TAG}" "https://github.com/NousResearch/hermes-agent.git" hermes-agent-src 2>&1 | tail -3
        else
            git clone --depth 1 --branch "${HERMES_TAG}" "https://github.com/NousResearch/hermes-agent.git" hermes-agent-src 2>&1 | tail -3
        fi
    fi
fi
SRC="$BUILD_DIR/hermes-agent-src"
echo "  源码目录: $SRC"
[ -d "$SRC/.git" ] && echo "  ✅ .git 存在 (hermes update 可用)" || echo "  ⚠️ .git 缺失"
# 确保 remote origin 指向 GitHub (本地 file:// clone 的 origin 指向本地路径, 需改回 GitHub 供 hermes update 用)
if [ -d "$SRC/.git" ]; then
    git -C "$SRC" remote set-url origin "https://github.com/NousResearch/hermes-agent.git" 2>/dev/null ||         git -C "$SRC" remote add origin "https://github.com/NousResearch/hermes-agent.git" 2>/dev/null || true
fi

echo "=== 2/5 创建 venv + editable 安装 hermes-agent 源码 (git checkout, 支持 hermes update) ==="
VENV_DIR="$BUILD_DIR/venv"
if [ ! -x "$VENV_DIR/bin/hermes" ]; then
    "$PY" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel
    # 把源码 checkout 移入 venv/src/hermes-agent — 这样打包 venv.tar.gz 时
    # 会自动包含源码 + .git, 部署后 editable 的 .pth 指向 venv/src/... 存在.
    # (editable 若源码在 venv 外, tar 只打 venv 目录会漏掉源码, 部署后无法加载)
    SRC_IN_VENV="${VENV_DIR}/src/hermes-agent"
    if [ -d "$SRC/.git" ] && [ "$SRC" != "$SRC_IN_VENV" ]; then
        echo "  源码 checkout 移入 venv: $SRC_IN_VENV"
        mkdir -p "${VENV_DIR}/src"
        rm -rf "$SRC_IN_VENV"
        cp -a "$SRC" "$SRC_IN_VENV"
        SRC="$SRC_IN_VENV"
    fi
    # editable 安装 hermes-agent 源码 checkout:
    #   - hermes_cli/ 等从源码加载 (非 wheel)
    #   - PROJECT_ROOT = 源码根 (含 .git) → hermes update 可 git pull
    #   - 与 31.31 原生安装(v0.20.1 editable) 方式一致
    echo "  editable 安装 hermes-agent 源码 (含 .git)..."
    "$VENV_DIR/bin/pip" install -e "$SRC" 2>&1 | tail -5 || {
        echo "  editable 安装失败, 尝试 --no-deps + 补依赖...";
        "$VENV_DIR/bin/pip" install -e "$SRC" --no-deps 2>&1 | tail -3 || true;
    }
    # 确保源码 checkout 的 .git 存在 (editable 保留在原位)
    if [ -d "$SRC/.git" ]; then
        echo "  .git 保留在源码根: $SRC/.git"
    fi
    # 标记 git 安装 (detect_install_method 识别为 'git')
    # editable 时 PROJECT_ROOT 是源码根, .install_method 写在源码根
    echo "git" > "$SRC/.install_method"
    # 依赖: 若 editable 已解析则跳过; 若 PyPI 慢, 用 PIP_PROXY=http://127.0.0.1:7890
    # 补装 gateway api_server 运行时依赖
    "$VENV_DIR/bin/pip" install aiohttp pyyaml cryptography 2>&1 | tail -2 || true
    if [ -n "${PIP_PROXY:-}" ]; then
        echo "  用代理补装缺失依赖 (PIP_PROXY=${PIP_PROXY})..."
        "$VENV_DIR/bin/pip" install --proxy "$PIP_PROXY" aiohttp pyyaml cryptography 2>&1 | tail -2 || true
    fi
fi
"$VENV_DIR/bin/hermes" --version

echo "=== 3/5 复用/准备前端 web_dist ==="
# 用 python 定位 site-packages 路径（可靠处理 glob）
SITE_PKG="$("$VENV_DIR/bin/python" -c 'import site,sys; print(site.getsitepackages()[0])')"
# editable 时 hermes_cli 在源码 (venv/src/hermes-agent), web_dist 目标指向源码
WEB_DIST_DEST="${SRC}/hermes_cli/web_dist"
echo "  web_dist 目标: ${WEB_DIST_DEST}"
mkdir -p "${WEB_DIST_DEST}"
# 优先复用本机现成 web_dist
if [ -d "$WEB_DIST_SRC" ] && [ -f "$WEB_DIST_SRC/index.html" ]; then
    echo "  复用现成 web_dist: $WEB_DIST_SRC"
    cp -r "$WEB_DIST_SRC"/* "${WEB_DIST_DEST}/" 2>/dev/null || true
    ls "${WEB_DIST_DEST}/index.html" >/dev/null 2>&1 && echo "  ✅ web_dist 已就位" || echo "  ⚠️ web_dist 复制可能不完整"
else
    echo "  无现成 web_dist，从源码构建前端（需要 npm）..."
    (cd "$SRC" && npm install --workspace web --no-audit --no-fund 2>/dev/null; cd web && npm run build 2>/dev/null || echo "  ⚠️ npm build 失败，可能无预构建前端")
    # 构建产物输出到 hermes_cli/web_dist
    if [ -d "${SRC}/hermes_cli/web_dist" ]; then
        cp -r "${SRC}/hermes_cli/web_dist"/* "${WEB_DIST_DEST}/" 2>/dev/null || true
    fi
fi

echo "=== 4/5 补全 bundled 资源 (wheel 缺失的 plugin.yaml/locales/skills 等) ==="
# wheel 构建不含 plugins/*/plugin.yaml、locales、skills 等非 .py 资源
# （pyproject package-data 只含 gateway assets）。从源码目录补进 site-packages。
BUNDLED_PLUGINS_SRC="${SRC}/plugins"
BUNDLED_PLUGINS_DEST="${SITE_PKG}/plugins"
if [ -d "$BUNDLED_PLUGINS_SRC" ] && [ -d "$BUNDLED_PLUGINS_DEST" ]; then
    echo "  补全 bundled plugins (plugin.yaml)..."
    # 复制所有 plugin.yaml（含子目录），不覆盖已有 .py
    (cd "$BUNDLED_PLUGINS_SRC" && find . -name "plugin.yaml" -exec cp --parents {} "$BUNDLED_PLUGINS_DEST/" \; 2>/dev/null) || true
    (cd "$BUNDLED_PLUGINS_SRC" && find . -type d -name "assets" -exec cp -r --parents {} "$BUNDLED_PLUGINS_DEST/" \; 2>/dev/null) || true
fi
# 补 locales / skills / optional-mcps / providers 等源码资源目录
for SUB in locales skills optional-mcps providers; do
    if [ -d "${SRC}/${SUB}" ] && [ -d "${SITE_PKG}/${SUB}" ]; then
        echo "  补全 ${SUB}/..."
        cp -r "${SRC}/${SUB}"/* "${SITE_PKG}/${SUB}/" 2>/dev/null || true
    fi
done
# 补 TUI workspace (dashboard Chat 依赖 ui-tui，wheel 不含)
if [ -d "${SRC}/ui-tui" ]; then
    echo "  补全 ui-tui/ (TUI workspace)..."
    rm -rf "${SITE_PKG}/ui-tui" 2>/dev/null
    cp -r "${SRC}/ui-tui" "${SITE_PKG}/ui-tui" 2>/dev/null || true
fi
# 补预构建 TUI bundle (dashboard Chat 的 _find_bundled_tui 用 hermes_cli/tui_dist/entry.js)
if [ -f "${SRC}/ui-tui/dist/entry.js" ]; then
    echo "  补全 hermes_cli/tui_dist/entry.js (预构建 TUI bundle)..."
    mkdir -p "${SITE_PKG}/hermes_cli/tui_dist"
    cp "${SRC}/ui-tui/dist/entry.js" "${SITE_PKG}/hermes_cli/tui_dist/entry.js" 2>/dev/null || true
fi

echo "=== 5/5 打包 venv.tar.gz ==="
rm -f "$VENV_TAR"
tar czf "$VENV_TAR" -C "$BUILD_DIR" venv
echo "  ✅ 产出: $VENV_TAR ($(du -h "$VENV_TAR" | cut -f1))"

echo "=== 6/5 完成 ==="
echo "下一步：把 app/venv.tar.gz 随仓库一起，在 NAS 上执行 bash scripts/build.sh 打 fpk。"
