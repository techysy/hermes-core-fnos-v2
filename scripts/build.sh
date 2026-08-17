#!/usr/bin/env bash
# HermesCore v2 打包脚本 — 版本号自动累加测试版第4位 + fnpack build + 交付
#
# 用法（在 NAS 构建目录 /vol1/1000/fnOS App/build/hermes-core-fnos-v2/ 运行）:
#   bash scripts/build.sh            # 自动累加第4位后打包
#   bash scripts/build.sh --formal   # 正式版：升第3位，去掉第4位（如 0.9.9.3 -> 0.10.0）
#
# 版本号单一来源：改 VERSION（三位基础），第4位由本脚本自动累加。
# 依赖：先在本机/构建机跑 scripts/prebuild.sh 产出 app/venv.tar.gz（含 v0.20.1 内核 + web_dist）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CUR_VER="$(cat "$ROOT/VERSION" 2>/dev/null | tr -d '[:space:]')"
[ -z "$CUR_VER" ] && CUR_VER="0.9.9"
FPK_DIR="/vol1/1000/fnOS App/fpk/HermesCore"
OLDFPK_DIR="/vol1/1000/fnOS App/old_fpk/HermesCore"

# --- 检查离线 venv 包是否存在 ---
OFFLINE_VENV="${ROOT}/app/venv.tar.gz"
if [ ! -f "$OFFLINE_VENV" ]; then
    echo "⚠️  未找到 app/venv.tar.gz（v0.20.1 预构建产物）。"
    echo "   请先在构建机执行: bash scripts/prebuild.sh 生成后再打包。"
    exit 1
fi
echo "✓ 离线 venv 已就绪: $OFFLINE_VENV ($(du -h "$OFFLINE_VENV" | cut -f1))"

# --- 计算版本号 ---
MODE="${1:-}"
if [ "$MODE" = "--formal" ]; then
    # 正式版：升第3位，去掉第4位
    IFS='.' read -ra P <<< "$CUR_VER"
    VER="${P[0]}.${P[1]}.$(( ${P[2]:-0} + 1 ))"
    echo "ℹ️  正式版：$CUR_VER -> $VER"
else
    # 测试版：第4位自动累加
    if [[ "$CUR_VER" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
        VER="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.${BASH_REMATCH[3]}.$((BASH_REMATCH[4] + 1))"
    else
        VER="${CUR_VER}.1"
    fi
    echo "ℹ️  测试版：$CUR_VER -> $VER"
fi

# --- 打包前确认 ---
echo "📦 即将打包版本：$VER"
if [ "${BUILD_AUTO:-0}" != "1" ]; then
    read -r -p "确认打包 $VER ? [y/N] " ans
    if [[ ! "$ans" =~ ^[Yy]$ ]]; then
        echo "已取消"; exit 1
    fi
fi

# --- 更新 manifest version + VERSION 文件为当前包版本 ---
sed -i "s/^version.*/version               = $VER/" "$ROOT/manifest"
echo "$VER" > "$ROOT/VERSION"
echo "✓ manifest + VERSION = $VER"

# --- fnpack build ---
(cd "$ROOT" && fnpack build >/dev/null 2>&1)
mv "$ROOT/HermesCore.fpk" "$ROOT/HermesCore-$VER.fpk"
echo "✓ 构建完成：HermesCore-$VER.fpk"

# --- 交付：旧包移 oldfpk，新包入 HermesCore/ ---
mkdir -p "$OLDFPK_DIR"
mv "$FPK_DIR"/HermesCore-*.fpk "$OLDFPK_DIR"/ 2>/dev/null || true
cp "$ROOT/HermesCore-$VER.fpk" "$FPK_DIR/"
rm -f "$ROOT/HermesCore-$VER.fpk"

echo "✓ 已交付：$FPK_DIR/HermesCore-$VER.fpk"
echo "✓ 旧包已归档：$OLDFPK_DIR/"
echo "当前测试版：$VER"
