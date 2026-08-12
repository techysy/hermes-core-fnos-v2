# CHANGELOG

## 0.9.9 (重构版起步)

> v2 全新骨架：内核升级到官方 Hermes Agent v0.20.0（GitHub 源码），前端预构建进 fpk，空壳 app 指向 :9119 WebUI 配置。

### 新增 / Added
- **内核升级到官方 v0.20.0** — 从 PyPI v0.19.0 切换为 GitHub v0.20.0 源码预构建，`pip install git+...@v2026.8.3`
- **前端预构建进 fpk** — 打包时预构建 web_dist 进 `venv.tar.gz`，dashboard 用 `--skip-build` 秒起，无需现场 npm
- **空壳 app 指向 :9119 WebUI** — 空壳 `hermes-dashboard-fnos` 桌面 iframe 指向 dashboard Web UI（配置入口）
- **预构建脚本 `scripts/prebuild.sh`** — python312 重建 venv + 复用/构建 web_dist，产出 `app/venv.tar.gz`

### 变更 / Changed
- 版本号体系：从 0.6.x 切换为 0.9.9.x（测试）→ 1.0.0（正式）
- 安装模式：在线 pip 装 PyPI v0.19.0 → 离线解压预构建 v0.20.0
- venv 构建：python3.11 → python312（cp312 C 扩展，适配 fnOS）
