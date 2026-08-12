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

# 内核版本（官方 tag）
HERMES_TAG="v2026.8.3"          # v0.20.0
HERMES_REPO="https://github.com/NousResearch/hermes-agent"
# 复用本机现成的 web_dist（若提供）
WEB_DIST_SRC="${WEB_DIST_SRC:-/home/yangyu/.hermes/hermes-agent/hermes_cli/web_dist}"

# Python 解释器（默认 python3.12，fnOS 需要 cp312 编译的 C 扩展）
PY="${PY:-}"
if [ -z "$PY" ]; then
    for cand in python3.12 /vol4/@appcenter/python312/bin/python3.12 /usr/bin/python3.12; do
        [ -x "$cand" ] && PY="$cand" && break
    done
fi
[ -z "$PY" ] && { echo "❌ 未找到 python3.12（fnOS 需要 cp312）。可 PY=/path/to/python3.12 指定。"; exit 1; }
echo "使用 Python: $PY"

mkdir -p "$BUILD_DIR" "$APP_DIR"

echo "=== 1/5 拉取 Hermes v0.20.0 源码 ==="
cd "$BUILD_DIR"
if [ ! -d "hermes-agent-src" ]; then
    curl -fsSL -o hermes-agent.tar.gz "https://codeload.github.com/NousResearch/hermes-agent/tar.gz/refs/tags/${HERMES_TAG}"
    tar xzf hermes-agent.tar.gz
    mv "hermes-agent-${HERMES_TAG#v}" hermes-agent-src 2>/dev/null || mv hermes-agent-* hermes-agent-src
fi
SRC="$BUILD_DIR/hermes-agent-src"
echo "  源码目录: $SRC"

echo "=== 2/5 创建 venv 并安装 v0.20.0 内核 ==="
VENV_DIR="$BUILD_DIR/venv"
if [ ! -x "$VENV_DIR/bin/hermes" ]; then
    "$PY" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel
    # v0.20.0 官方禁止非 editable 构建 wheel，需设 HERMES_NIX_BUILD=1 绕过 guard，
    # 构建 wheel 后安装（非 editable，避免路径依赖，可移植到 fnOS）
    echo "  构建 hermes v0.20.0 wheel 并安装..."
    (cd "$SRC" && HERMES_NIX_BUILD=1 "$VENV_DIR/bin/pip" wheel . --no-deps -w "$BUILD_DIR/wheels" >/dev/null 2>&1)
    "$VENV_DIR/bin/pip" install "$BUILD_DIR"/wheels/hermes_agent-*.whl 2>&1 | tail -2 || {
        echo "  wheel 安装失败，尝试从 GitHub 源码安装...";
        HERMES_NIX_BUILD=1 "$VENV_DIR/bin/pip" install "git+https://github.com/NousResearch/hermes-agent@${HERMES_TAG}" 2>&1 | tail -2;
    }
fi
"$VENV_DIR/bin/hermes" --version

echo "=== 3/5 复用/准备前端 web_dist ==="
# 用 python 定位 site-packages 路径（可靠处理 glob）
SITE_PKG="$("$VENV_DIR/bin/python" -c 'import site,sys; print(site.getsitepackages()[0])')"
WEB_DIST_DEST="${SITE_PKG}/hermes_cli/web_dist"
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
    # 构建产物输出到 hermes_cli/web_dist，复制到 venv 内
    if [ -d "${SRC}/hermes_cli/web_dist" ]; then
        cp -r "${SRC}/hermes_cli/web_dist"/* "${WEB_DIST_DEST}/" 2>/dev/null || true
    fi
fi

echo "=== 4/5 打包 venv.tar.gz ==="
rm -f "$VENV_TAR"
tar czf "$VENV_TAR" -C "$BUILD_DIR" venv
echo "  ✅ 产出: $VENV_TAR ($(du -h "$VENV_TAR" | cut -f1))"

echo "=== 5/5 完成 ==="
echo "下一步：把 app/venv.tar.gz 随仓库一起，在 NAS 上执行 bash scripts/build.sh 打 fpk。"
