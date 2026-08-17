#!/usr/bin/env python3
"""Hermes Core 状态页 + 配置服务.
提供:
- GET /            → 状态页 + 配置表单 (脱敏显示当前配置)
- POST /api/config → 保存配置到 gateway.env (需 Bearer API key 鉴权)
- POST /api/restart → 重启内核 (需 Bearer API key 鉴权)
纯 stdlib 零依赖. 监听独立端口 (默认 8648).
"""
import base64
import hashlib
import json
import os
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 可选: PTY 终端 (Hermes venv 提供 hermes_cli.pty_bridge + ptyprocess)
try:
    from hermes_cli.pty_bridge import PtyBridge, PtyUnavailableError
    _PTY_OK = True
except Exception:  # pragma: no cover - 非 Hermes venv
    PtyBridge = None
    PtyUnavailableError = RuntimeError
    _PTY_OK = False

CORE_PORT = os.environ.get("CORE_PORT", "8642")
CORE_HOST = "127.0.0.1"
API_KEY = os.environ.get("CORE_API_KEY", "")
LISTEN_PORT = int(os.environ.get("STATUS_PORT", "8648"))
BIND_HOST = os.environ.get("STATUS_HOST", "0.0.0.0")
CONFIG_FILE = os.environ.get("CORE_CONFIG", "")
CMD_MAIN = os.environ.get("CORE_CMD", "")
# 官方统一网关: Unix socket (gatewaySocket 文件名, 放 ${TRIM_APPDEST}/target/)
SOCK_PATH = os.environ.get("STATUS_SOCK", "")  # 空 = 不监听 Unix socket
# hermes 可执行 (容器终端里把其目录加入 PATH, 方便运行 hermes model/init 等命令)
HERMES_BIN = os.environ.get("HERMES_BIN", "")

# ── 本地静态资源 (xterm.js 等, 避免 CDN 依赖) ─────────────────────────────
# status_server.py 所在目录 (cmd/), vendor 资源放 cmd/vendor/
_HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR_DIR = os.path.join(_HERE, "vendor")
VENDOR_TYPES = {
    ".js": "application/javascript",
    ".css": "text/css",
    ".map": "application/json",
}


# ── 应用版本 (与 manifest 一致, 单一来源 app 目录; 参考 Strava/hugo 品牌区) ──
def _load_app_version():
    """从 fpk manifest 读取版本号 (参考 Strava/hugo: manifest + VERSION 单一来源)."""
    for cand in (
        os.path.join(os.path.dirname(CMD_MAIN), "..", "manifest") if CMD_MAIN else "",
        "/var/apps/HermesCore/manifest",
    ):
        if cand and os.path.isfile(cand):
            try:
                for line in open(cand, encoding="utf-8", errors="replace"):
                    if line.strip().lower().startswith("version"):
                        return line.split("=")[1].strip()
            except OSError:
                continue
    return "0.6.0"

APP_VERSION = _load_app_version()


# ── 日志 (参考 hugo-blog 控制台: 多源 + 按需 tail) ──────────────────────
LOG_DIR = os.path.dirname(CONFIG_FILE) if CONFIG_FILE else ""
LOG_SOURCES = {
    "core": os.path.join(LOG_DIR, "core.log"),
    "status": os.path.join(LOG_DIR, "status.log"),
    "dashboard": os.path.join(LOG_DIR, "dashboard.log"),
    "install": os.path.join(LOG_DIR, "install.log"),
}
LOG_LABELS = {"core": "内核日志", "status": "状态页日志", "dashboard": "Dashboard 日志", "install": "安装日志"}


def _read_log_source(name, tail=300):
    """读取某日志源尾部 N 行."""
    path = LOG_SOURCES.get(name, "")
    if not path or not os.path.isfile(path):
        return "", False
    try:
        size = os.path.getsize(path)
        # 只读尾部 (避免超大文件)
        with open(path, "rb") as f:
            if size > 512 * 1024:
                f.seek(size - 512 * 1024)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        if tail > 0:
            lines = text.rstrip("\n").split("\n")
            text = "\n".join(lines[-tail:])
        return text, True
    except OSError:
        return "", False


# ── WebSocket 帧编解码 (stdlib 手写, 服务端) ─────────────────────────────
def _ws_accept_key(key):
    """WebSocket 握手 Sec-WebSocket-Accept 计算."""
    return base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()


def _ws_encode(data, opcode=0x1):
    """服务端 → 客户端帧 (不掩码). 支持 text/binary/close/ping/pong."""
    header = bytearray([0x80 | opcode])
    length = len(data)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack(">H", length)
    else:
        header.append(127)
        header += struct.pack(">Q", length)
    return bytes(header) + data


def _ws_recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return data
        data += chunk
    return data


def _ws_read_frame(sock):
    """读一帧 (客户端掩码帧). 返回 (opcode, payload_bytes) 或 None(连接关闭)."""
    head = _ws_recv_exact(sock, 2)
    if len(head) < 2:
        return None
    b1, b2 = head[0], head[1]
    opcode = b1 & 0x0F
    fin = b1 & 0x80
    masked = b2 & 0x80
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _ws_recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _ws_recv_exact(sock, 8))[0]
    mask = _ws_recv_exact(sock, 4) if masked else None
    payload = _ws_recv_exact(sock, length)
    if mask and len(mask) == 4:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return (opcode, payload)


def find_shell():
    """定位容器内可用的 shell (bash 优先, 回退 sh)."""
    for cand in ("/bin/bash", "/usr/bin/bash", "/bin/sh"):
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            return cand
    return "sh"


def _handle_pty_ws(conn, sock, headers):
    """PTY 终端 WebSocket 主循环. conn=已握手 socket, headers=网关用户 Header."""
    print(f"[pty] _PTY_OK={_PTY_OK} HERMES_HOME={os.environ.get('HERMES_HOME','')!r}", flush=True)
    if not _PTY_OK:
        try:
            sock.sendall(_ws_encode(b"\r\n\x1b[31mTerminal unavailable: Hermes PTY not available. Enable python312 runtime.\x1b[0m\r\n"))
        except Exception:
            pass
        try:
            sock.sendall(_ws_encode(b"", 0x8))  # close
        except Exception:
            pass
        return

    # spawn 容器内 shell (bash, 回退 sh) via PTY — 替代废弃的 hermes --tui 终端.
    # 终端在容器内跑普通 shell, 不再依赖 hermes/node/ui-tui, 体验更轻快.
    argv = [find_shell()]
    env = os.environ.copy()
    # 确保工作目录 HERMES_HOME 非空: 环境变量缺失时从 CORE_CONFIG(gateway.env 所在目录) 推导
    hh = os.environ.get("HERMES_HOME", "")
    if not hh:
        _cfg_dir = os.path.dirname(CONFIG_FILE) if CONFIG_FILE else ""
        hh = os.path.join(_cfg_dir, "hermes_home") if _cfg_dir else "/tmp/hermes_home"
        os.environ["HERMES_HOME"] = hh
        os.makedirs(hh, exist_ok=True)
    env.setdefault("HERMES_HOME", hh)
    # 确保 UTF-8 locale (中文输入/输出正常)
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("LC_ALL", "C.UTF-8")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # 把 hermes 可执行所在目录加入 PATH, 让用户在容器终端里直接敲 hermes model/init 等命令
    if HERMES_BIN:
        _bin_dir = os.path.dirname(HERMES_BIN)
        if _bin_dir and os.path.isdir(_bin_dir):
            env["PATH"] = _bin_dir + os.pathsep + env.get("PATH", "")
    try:
        bridge = PtyBridge.spawn(argv, cwd=hh, env=env, cols=80, rows=24)
        print(f"[pty] spawn OK shell={argv[0]!r} cwd={hh!r}", flush=True)
    except (PtyUnavailableError, OSError, FileNotFoundError) as exc:
        print(f"[pty] spawn FAILED: {exc}", flush=True)
        try:
            sock.sendall(_ws_encode(("\r\n\x1b[31mTerminal failed to start: %s\x1b[0m\r\n" % exc).encode()))
            sock.sendall(_ws_encode(b"", 0x8))
        except Exception:
            pass
        return

    def pump_pty_to_ws():
        sent = 0
        while True:
            try:
                chunk = bridge.read(timeout=0.5)
            except Exception as e:
                print(f"[pty] read err {e}", flush=True)
                chunk = None
            if chunk is None:
                print(f"[pty] read None (exited), sent={sent}", flush=True)
                break
            if not chunk:
                continue
            sent += len(chunk)
            try:
                sock.sendall(_ws_encode(chunk))
            except Exception as e:
                print(f"[pty] send err {e}", flush=True)
                break
        print(f"[pty] pump done, sent={sent}", flush=True)
        try:
            sock.sendall(_ws_encode(b"", 0x8))  # close frame
        except Exception:
            pass

    # 记录 PTY 是否已退出 (pump 线程结束时置位)
    pty_done = [False]
    # 包装 pump 线程, 结束时标记
    orig_pump = pump_pty_to_ws
    def wrapped_pump():
        try:
            orig_pump()
        finally:
            pty_done[0] = True
    t = threading.Thread(target=wrapped_pump, daemon=True)
    t.start()
    try:
        while True:
            try:
                frame = _ws_read_frame(sock)
            except socket.timeout:
                # 前端暂无输入: 保持连接, 若 PTY 已退出则结束
                if pty_done[0]:
                    break
                continue
            except Exception:
                break
            if frame is None:
                break
            opcode, payload = frame
            if opcode == 0x8:  # close
                break
            if opcode == 0x9:  # ping → pong
                try:
                    sock.sendall(_ws_encode(payload, 0xA))
                except Exception:
                    pass
                continue
            if opcode == 0xA:  # pong
                continue
            # resize escape \x1b[<rows>;<cols>R (xterm 风格) 或自定义
            if payload.startswith(b"\x1b[") and payload.endswith(b"R"):
                m = re.match(rb"\x1b\[(\d+);(\d+)R", payload)
                if m:
                    try:
                        bridge.resize(cols=int(m.group(2)), rows=int(m.group(1)))
                    except Exception:
                        pass
                    continue
            try:
                bridge.write(payload)
            except Exception:
                break
    except Exception:
        pass
    finally:
        try:
            bridge.close()
        except Exception:
            pass

# 状态页 UI 版本 — 动态从已安装 manifest 读取 (替代硬编码, 升级即更新)
# 读不到时回退为空 (footer 不显示版本号)
def _app_version():
    """从已安装的 manifest 动态读取 HermesCore 应用版本.
    返回如 '0.4.6', 读不到时返回 '' (footer 不显示版本号).
    """
    candidates = []
    if CMD_MAIN:
        # CMD_MAIN 指向 <app_dir>/cmd/main → manifest 在上级目录
        candidates.append(os.path.join(os.path.dirname(os.path.dirname(CMD_MAIN)), "manifest"))
    candidates += [
        "/var/apps/HermesCore/manifest",
        "/vol4/@appcenter/HermesCore/manifest",
    ]
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("version") and "=" in line:
                        return line.split("=", 1)[1].strip()
        except (OSError, IOError):
            continue
    return ""


STATUS_VER = _app_version()

# 可配置字段: (gateway.env key, 表单 label, 是否敏感, 分组)
# 分组: core=内核 / llm=LLM连接 / dash=Dashboard / feishu=飞书 / wechat=微信 / qq=QQ / dingtalk=钉钉 / proxy=代理
CONFIG_FIELDS = [
    ("API_SERVER_HOST", "监听地址", False, "core"),
    ("API_SERVER_PORT", "API 端口", False, "core"),
    ("API_SERVER_KEY", "API Key", True, "core"),
    ("ROUTER_API_KEY", "9Router API Key", True, "llm"),
    ("DEEPSEEK_API_KEY", "DeepSeek API Key", True, "llm"),
    ("XIAOMI_API_KEY", "Xiaomi MiMo API Key", True, "llm"),
    ("LLM_BASE_URL", "默认 LLM Base URL", False, "llm"),
    ("LLM_API_KEY", "默认 LLM Token", True, "llm"),
    ("LLM_MODEL", "默认模型名", False, "llm"),
    ("DASHBOARD_ENABLED", "Dashboard 开关", False, "dash"),
    ("DASHBOARD_USER", "Dashboard 用户名", False, "dash"),
    ("DASHBOARD_PASSWORD", "Dashboard 密码", True, "dash"),
    ("HTTP_PROXY", "HTTP 代理地址", False, "proxy"),
    ("HTTPS_PROXY", "HTTPS 代理地址", False, "proxy"),
    ("NO_PROXY", "NO_PROXY 例外", False, "proxy"),
    ("FEISHU_APP_ID", "飞书应用 App ID", False, "feishu"),
    ("FEISHU_APP_SECRET", "飞书应用 Secret", True, "feishu"),
    ("FEISHU_VERIFICATION_TOKEN", "飞书验证 Token(验证码)", True, "feishu"),
    ("FEISHU_ENCRYPT_KEY", "飞书加密 Key", True, "feishu"),
    ("WEIXIN_ACCOUNT_ID", "微信账号 ID", False, "wechat"),
    ("WEIXIN_TOKEN", "微信 Token(验证码)", True, "wechat"),
    ("QQ_APP_ID", "QQ 机器人 App ID", False, "qq"),
    ("QQ_APP_SECRET", "QQ 机器人 Secret", True, "qq"),
    ("DINGTALK_CLIENT_ID", "钉钉 Client ID", False, "dingtalk"),
    ("DINGTALK_CLIENT_SECRET", "钉钉 Client Secret", True, "dingtalk"),
]

# 配置分组标签
CONFIG_GROUPS = {
    "core": ("🔧 内核", "core"),
    "llm": ("🧠 LLM 连接", "llm"),
    "dash": ("📊 Dashboard", "dash"),
    "proxy": ("🌐 代理", "proxy"),
    "feishu": ("💬 飞书", "feishu"),
    "wechat": ("💬 微信", "wechat"),
    "qq": ("🐧 QQ", "qq"),
    "dingtalk": ("📌 钉钉", "dingtalk"),
}

# 模型供应商 (参考 9Router providers 页)
# key: 标识, name: 名称, ico: 图标, env: API key 的 gateway.env 字段, default: 是否默认
# bg: 图标背景色, desc: 描述
MODEL_PROVIDERS = [
    {"key": "9router", "name": "9Router", "ico": "🔧", "env": "ROUTER_API_KEY",
     "default": True, "local": True, "bg": "#2f6fed", "desc": "本地代理 (本机 :20128)"},
    {"key": "deepseek", "name": "DeepSeek", "ico": "🐋", "env": "DEEPSEEK_API_KEY",
     "default": False, "local": False, "bg": "#4d6bfe", "desc": "DeepSeek API"},
    {"key": "mimo", "name": "Xiaomi MiMo", "ico": "📱", "env": "XIAOMI_API_KEY",
     "default": False, "local": False, "bg": "#ff6900", "desc": "Xiaomi MiMo API"},
]

# 供应商 base_url (用于生成 config.yaml custom_providers)
MODEL_PROVIDER_URLS = {
    "9router": "http://127.0.0.1:20128/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "mimo": "https://api.xiaomimimo.com/v1",
}


def _mask(v):
    """脱敏: 保留前后几位, 中间省略."""
    v = v or ""
    if len(v) <= 6:
        return "***"
    return v[:3] + "..." + v[-3:]


def _load_config():
    """读取 gateway.env. 返回 dict."""
    cfg = {}
    if CONFIG_FILE and os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    cfg[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            pass
    return cfg


def _save_config(data):
    """写 gateway.env. 只更新前端提交的字段, 未提交的保留原值 (避免误清空).

    兼容性: 只重写 CONFIG_FIELDS 白名单内的字段; gateway.env 中所有不在白名单
    的自定义字段 (如 install_callback/其他工具追加的 EXTRA_ENV、未来新增 key 等)
    原样保留, 绝不因状态页保存而丢失。
    """
    if not CONFIG_FILE:
        return False, "CONFIG_FILE 未配置"
    try:
        # 读当前值, 未提交的字段保留原值
        current = _load_config()
        # 保留 gateway.env 中不在 CONFIG_FIELDS 白名单的自定义字段 (原顺序/原值)
        managed_keys = {k for k, _, _, _ in CONFIG_FIELDS}
        extra_lines = []
        try:
            with open(CONFIG_FILE) as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _k = _line.split("=", 1)[0].strip()
                    if _k not in managed_keys:
                        extra_lines.append(_line)
        except Exception:
            pass
        # Dashboard 联动: 配置了用户名/密码则自动启用 (无需单独开开关)
        if data.get("DASHBOARD_USER", "").strip() or data.get("DASHBOARD_PASSWORD", "").strip():
            data = dict(data)
            data["DASHBOARD_ENABLED"] = "true"
        with open(CONFIG_FILE, "w") as f:
            for key, _, _sens, _grp in CONFIG_FIELDS:
                if key in data:
                    # 前端提交了 → 用新值 (留空 = 清空该字段)
                    val = data.get(key, "").strip()
                else:
                    # 前端没提交 → 保留原值
                    val = current.get(key, "")
                f.write(f'{key}="{val}"\n')
            # 追加保留的自定义字段 (不在白名单内)
            for el in extra_lines:
                f.write(el + "\n")
        os.chmod(CONFIG_FILE, 0o600)
        return True, "saved"
    except Exception as e:
        return False, str(e)


def _do_restart():
    """调用 cmd/main restart."""
    if not CMD_MAIN or not os.path.exists(CMD_MAIN):
        return False, "cmd/main 未配置"
    try:
        env = os.environ.copy()
        env["TRIM_APPNAME"] = os.environ.get("TRIM_APPNAME", "HermesCore")
        # 后台执行 restart (延迟, 避免杀掉自己)
        subprocess.Popen(["bash", CMD_MAIN, "restart"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, "restarting"
    except Exception as e:
        return False, str(e)


def _chat_proxy(messages, stream=False):
    """代理聊天请求到本机 api_server (8642) 的 /v1/chat/completions.
    stream=True 时返回生成器 (逐块 yield 文本); 否则返回完整 reply.
    """
    if not messages:
        if stream:
            return iter([]), None
        return None, "no messages"
    cfg = _load_config()
    api_key = cfg.get("API_SERVER_KEY", "")
    model = cfg.get("LLM_MODEL", "") or "default"
    base = os.environ.get("CORE_HOST", "127.0.0.1")
    port = os.environ.get("CORE_PORT", "8642")
    url = f"http://{base}:{port}/v1/chat/completions"
    body = json.dumps({"model": model, "messages": messages, "stream": stream}).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        resp = urllib.request.urlopen(req, timeout=300)
    except Exception as e:
        if stream:
            return iter([]), f"连接失败: {e}"
        return None, str(e)

    if not stream:
        try:
            data = json.loads(resp.read().decode())
            reply = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return reply, None
        except Exception as e:
            return None, f"解析失败: {e}"

    # 流式: 生成器逐行解析 SSE, yield 文本增量
    captured_model = [""]

    def gen():
        try:
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                    if not captured_model[0]:
                        captured_model[0] = chunk.get("model", "") or model
                    delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    if delta:
                        yield delta
                except Exception:
                    continue
        except Exception:
            pass

    return gen(), None, captured_model


def _core_health():
    try:
        req = urllib.request.Request(f"http://{CORE_HOST}:{CORE_PORT}/health",
                                     headers={"Authorization": f"Bearer {API_KEY}"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            return True, data
    except Exception as e:
        return False, {"error": str(e)}


def _gateway_status():
    """读取 /health/detailed, 返回消息网关 + 平台状态."""
    try:
        req = urllib.request.Request(f"http://{CORE_HOST}:{CORE_PORT}/health/detailed",
                                     headers={"Authorization": f"Bearer {API_KEY}"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode())
        gw_state = data.get("gateway_state", "unknown")
        platforms = data.get("platforms", {}) or {}
        connected = [k for k, v in platforms.items() if isinstance(v, dict) and v.get("state") == "connected"]
        return {
            "state": gw_state,
            "platforms": platforms,
            "connected": connected,
            "raw": data,
        }
    except Exception as e:
        return {"state": "unknown", "error": str(e), "platforms": {}, "connected": []}


def _dashboard_status():
    """探测 Hermes 原生 dashboard (9119) 状态.

    注意: cmd/main 里 dashboard 默认是启用的 (DASHBOARD_ENABLED:-true),
    gateway.env 通常不显式写 DASHBOARD_ENABLED — 所以默认值必须是 True,
    否则实际在跑也会显示"未启用" (2026-08-12 踩坑)。
    """
    cfg = _load_config()
    # 与 cmd/main 的 ${DASHBOARD_ENABLED:-true} 对齐: 未显式配置 = 启用
    enabled = cfg.get("DASHBOARD_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    user = cfg.get("DASHBOARD_USER", "") or "admin"
    port = 9119
    ok = False
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/login", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            ok = resp.status == 200
    except Exception:
        ok = False
    return {
        "enabled": enabled,
        "ok": ok,
        "user": user,
        "port": port,
    }


def _llm_status():
    """探测默认 LLM API 连接状态 (LLM_BASE_URL/v1/models)."""
    cfg = _load_config()
    base = cfg.get("LLM_BASE_URL", "")
    key = cfg.get("LLM_API_KEY", "")
    model = cfg.get("LLM_MODEL", "")
    if not base:
        # 未配置兜底, 显示为"未配置" (用 9Router 或无)
        return {"configured": False, "ok": False, "msg": "未配置默认 LLM（默认使用 9Router）"}
    url = base.rstrip("/") + "/v1/models"
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            models = [m.get("id", "") for m in data.get("data", [])][:5] if isinstance(data.get("data"), list) else []
            return {"configured": True, "ok": True, "msg": f"连接正常 ({len(data.get('data', []))} 模型)", "model": model, "models": models}
    except Exception as e:
        return {"configured": True, "ok": False, "msg": f"连接失败: {e}", "model": model}


PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hermes Core</title>
<style>
  /* 日夜主题 CSS 变量 */
  :root, [data-theme="light"] {{
    --bg: #f0f2f5; --card: #ffffff; --text: #222222; --muted: #6b7280;
    --border: #e5e7eb; --input-bg: #ffffff; --accent: #1677ff;
    --ok-bg: #e6f4ff; --ok-text: #0e9f4e; --down-bg: #fff1f0; --down-text: #d93026;
    --tab-bg: #f0f2f5; --tab-active: #ffffff; --shadow: rgba(0,0,0,.08);
  }}
  [data-theme="dark"] {{
    --bg: #141414; --card: #1f1f1f; --text: #e8e8ea; --muted: #9a9aa0;
    --border: #3a3a44; --input-bg: #26262e; --accent: #4d8dff;
    --ok-bg: #16324f; --ok-text: #34c673; --down-bg: #3a1d1d; --down-text: #ff7a70;
    --tab-bg: #2e2e36; --tab-active: #1f1f1f; --shadow: rgba(0,0,0,.3);
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; background:var(--bg); color:var(--text); margin:0; -webkit-text-size-adjust:100%; }}
  /* 侧边栏布局 (参考 9Router) */
  .layout {{ display:flex; min-height:100vh; }}
  .sidebar {{ width:200px; background:var(--card); border-right:1px solid var(--border); padding:16px 10px; flex-shrink:0; }}
  .sidebar .brand {{ font-size:15px; font-weight:700; padding:4px 12px 14px; border-bottom:1px solid var(--border); margin-bottom:10px; display:flex; flex-direction:column; align-items:flex-start; }}
  .sidebar .brand-ver {{ font-size:11px; font-weight:400; color:var(--muted); line-height:1; margin-top:4px; }}
  .nav-item {{ display:flex; align-items:center; gap:8px; padding:10px 12px; border-radius:8px; cursor:pointer; font-size:13px; color:var(--muted); margin-bottom:2px; position:relative; }}
  .nav-item:hover {{ background:var(--card); }}
  .nav-item.active {{ background:var(--ok-bg); color:var(--accent); font-weight:600; }}
  .nav-item.active::before {{ content:""; position:absolute; left:-10px; top:8px; bottom:8px; width:3px; border-radius:2px; background:var(--accent); }}
  .nav-item .ico {{ font-size:15px; }}
  .nav-section {{ font-size:11px; color:var(--muted); padding:12px 12px 4px; text-transform:uppercase; letter-spacing:.5px; }}
  .main {{ flex:1; padding:16px; min-width:0; }}
  .topbar {{ display:flex; align-items:center; justify-content:space-between; margin-bottom:14px; gap:8px; }}
  .topbar h1 {{ font-size:20px; margin:0; }}
  .topbar-actions {{ display:flex; gap:8px; }}
  .icon-btn {{ padding:8px 12px; margin-top:0; border:1px solid var(--border); border-radius:8px; background:var(--card); color:var(--text); font-size:13px; cursor:pointer; }}
  .icon-btn:hover {{ opacity:.85; }}
  .topbar-menu {{ position:relative; }}
  .menu-dropdown {{ position:absolute; right:0; top:calc(100% + 6px); background:var(--card); border:1px solid var(--border); border-radius:10px; box-shadow:0 4px 16px var(--shadow); min-width:140px; z-index:100; padding:4px; }}
  .menu-item {{ display:flex; align-items:center; gap:8px; padding:9px 12px; border-radius:6px; cursor:pointer; font-size:13px; color:var(--text); white-space:nowrap; }}
  .menu-item:hover {{ background:var(--ok-bg); }}
  .menu-item .menu-ico {{ font-size:14px; }}
  .menu-item.menu-danger {{ color:var(--down-text); }}
  .hamburger {{ display:none; padding:8px 12px; margin-top:0; border:1px solid var(--border); border-radius:8px; background:var(--card); color:var(--text); font-size:16px; cursor:pointer; }}
  .sidebar-overlay {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,.4); z-index:40; }}
  /* 移动端: 汉堡收起侧栏 */
  @media (max-width: 768px) {{
    .hamburger {{ display:block; }}
    .sidebar {{ position:fixed; left:-200px; top:0; bottom:0; z-index:50; transition:left .2s; }}
    .sidebar.open {{ left:0; }}
    .sidebar-overlay.show {{ display:block; }}
    .main {{ padding:12px; }}
  }}
  .card {{ background:var(--card); border-radius:12px; padding:20px; margin-bottom:12px; box-shadow:0 1px 4px var(--shadow); }}
  /* 配置区块 — 分组独立卡片 */
  .cfg-section {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px 18px; margin-bottom:14px; }}
  .cfg-section-title {{ display:flex; align-items:center; gap:8px; font-size:14px; font-weight:700; color:var(--text); margin-bottom:12px; padding-bottom:8px; border-bottom:1px solid var(--border); }}
  .cfg-section .hint {{ font-size:11px; color:var(--muted); margin-top:10px; }}
  .cfg-section input {{ margin-bottom:2px; }}
  /* 配置字段：内核监听地址+端口 一排 */
  .field-row {{ display:flex; gap:12px; }}
  .field-col {{ flex:1; min-width:0; }}
  .field-col label {{ display:block; font-size:12px; color:var(--muted); margin-bottom:4px; }}
  .field-col input {{ width:100%; }}
  /* Dashboard 开关 (switch) */
  .switch-wrap {{ display:flex; align-items:center; gap:10px; margin-bottom:2px; }}
  .switch {{ position:relative; width:48px; height:26px; border-radius:13px; background:var(--border); border:none; cursor:pointer; transition:background .2s; padding:0; }}
  .switch .knob {{ position:absolute; top:3px; left:3px; width:20px; height:20px; border-radius:50%; background:#fff; transition:left .2s; box-shadow:0 1px 3px rgba(0,0,0,.3); }}
  .switch.on {{ background:var(--accent); }}
  .switch.on .knob {{ left:25px; }}
  .switch-text {{ font-size:13px; color:var(--text); }}
  /* 默认模型卡片 */
  .dm-current {{ font-size:13px; color:var(--text); background:var(--input-bg); border:1px solid var(--border); border-radius:8px; padding:8px 10px; margin-bottom:10px; word-break:break-all; }}
  .dm-input {{ width:100%; padding:8px 10px; border:1px solid var(--border); border-radius:8px; background:var(--input-bg); color:var(--text); font-size:13px; box-sizing:border-box; }}
  select.dm-input {{ cursor:pointer; }}
  /* 聊天窗口 */
  .chat-card {{ display:flex; flex-direction:column; height:calc(100vh - 140px); min-height:400px; }}
  .chat-msgs {{ flex:1; overflow-y:auto; padding:10px; background:var(--input-bg); border-radius:8px; margin-bottom:10px; }}
  .chat-msg {{ margin-bottom:8px; max-width:70%; width:fit-content; min-width:40px; padding:8px 12px; font-size:14px; line-height:1.5; word-break:break-word; overflow-wrap:break-word; white-space:pre-wrap; }}
  /* 微信风格: 自己(右,绿) / 对方(左,白) */
  .chat-msg.user {{ margin-left:auto; background:#95ec69; color:#000; border-top-right-radius:4px; border-top-left-radius:12px; border-bottom-left-radius:12px; border-bottom-right-radius:12px; }}
  .chat-msg.assistant {{ margin-right:auto; background:var(--card); color:var(--text); border:1px solid var(--border); border-top-left-radius:4px; border-top-right-radius:12px; border-bottom-left-radius:12px; border-bottom-right-radius:12px; }}
  .chat-msg.error {{ margin-right:auto; background:var(--down-bg); color:var(--down-text); border-top-left-radius:4px; border-top-right-radius:12px; border-bottom-left-radius:12px; border-bottom-right-radius:12px; }}
  .chat-msg .role {{ font-size:11px; color:var(--muted); margin-bottom:2px; }}
  /* 消息底部元信息 (耗时 · 模型) — 小号灰色, 与正文隔断 */
  .msg-meta {{ font-size:11px; color:var(--muted); margin-top:6px; padding-top:5px; border-top:1px solid var(--border); opacity:.75; white-space:nowrap; }}
  .chat-input-row {{ display:flex; gap:8px; align-items:flex-end; }}
  .chat-input {{ flex:1; padding:10px; border:1px solid var(--border); border-radius:8px; background:var(--input-bg); color:var(--text); font-size:14px; resize:vertical; }}
  .chat-send {{ width:44px; height:44px; border-radius:50%; background:#95ec69; color:#fff; font-size:18px; display:flex; align-items:center; justify-content:center; cursor:pointer; border:none; transition:opacity .15s; }}
  .chat-send:hover {{ opacity:.85; }}
  .chat-send:active {{ opacity:.7; }}
  .chat-send svg {{ width:22px; height:22px; }}
  @media (max-width: 480px) {{ .chat-card {{ height:calc(100vh - 100px); }} }}
  /* 模型供应商卡片网格 (参考 9Router providers) */
  .providers-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:12px; }}
  .provider-card {{ background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px; cursor:pointer; transition:border-color .15s; position:relative; }}
  .provider-card:hover {{ border-color:var(--accent); }}
  .provider-card .p-ico {{ width:40px; height:40px; border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:20px; margin-bottom:10px; }}
  .provider-card .p-name {{ font-size:14px; font-weight:600; margin-bottom:4px; }}
  .provider-card .p-status {{ font-size:12px; display:inline-flex; align-items:center; gap:4px; padding:3px 10px; border-radius:20px; margin-top:6px; }}
  .provider-card .p-status.connected {{ background:var(--ok-bg); color:var(--ok-text); }}
  .provider-card .p-status.pending {{ background:var(--down-bg); color:var(--down-text); }}
  .provider-card .p-badge {{ position:absolute; top:10px; right:10px; font-size:11px; padding:2px 8px; border-radius:10px; background:var(--accent); color:#fff; }}
  .provider-card .p-desc {{ font-size:11px; color:var(--muted); margin-top:6px; }}
  /* 消息平台卡片网格 (参考供应商页) — 卡片加高, 编辑用绝对定位覆盖层不撑开 */
  .msg-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:12px; }}
  .msg-card {{ position:relative; background:var(--card); border:1px solid var(--border); border-radius:12px; padding:16px; cursor:pointer; transition:border-color .15s; min-height:150px; display:flex; flex-direction:column; }}
  .msg-card:hover {{ border-color:var(--accent); }}
  .msg-card .msg-card-ico {{ width:40px; height:40px; border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:20px; margin-bottom:10px; }}
  .msg-card .msg-card-name {{ font-size:14px; font-weight:600; margin-bottom:4px; }}
  .msg-card .msg-card-status {{ font-size:12px; display:inline-flex; padding:3px 10px; border-radius:20px; margin-top:6px; background:var(--down-bg); color:var(--down-text); align-self:flex-start; }}
  .msg-card .msg-card-desc {{ font-size:11px; color:var(--muted); margin-top:auto; padding-top:8px; }}
  /* 平台配置覆盖层 (绝对定位, 同供应商 p-edit) */
  .msg-edit {{ position:absolute; top:0; left:0; right:0; bottom:0; z-index:5; background:var(--card); border-radius:12px; padding:12px; overflow-y:auto; box-shadow:0 2px 12px var(--shadow); }}
  .msg-edit-title {{ font-size:12px; font-weight:600; margin-bottom:8px; }}
  .msg-edit label {{ font-size:11px; color:var(--muted); margin:6px 0 2px; }}
  .msg-edit input {{ width:100%; padding:6px 8px; border:1px solid var(--border); border-radius:6px; font-size:12px; box-sizing:border-box; background:var(--input-bg); color:var(--text); margin-bottom:4px; }}
  .msg-qr-btn {{ margin:4px 0 6px; padding:6px 10px; border:none; border-radius:6px; background:var(--accent); color:#fff; font-size:12px; cursor:pointer; }}
  @media (max-width: 600px) {{ .msg-grid {{ grid-template-columns:1fr 1fr; }} }}
  /* 卡片内联配置区 (响应式) */
  /* 卡片内联配置区 — 绝对定位覆盖在原卡片容器上, 不改卡片高度 (网格不被撑大) */
  .p-edit {{ position:absolute; top:0; left:0; right:0; bottom:0; z-index:5; background:var(--card); border-radius:12px; padding:12px; display:flex; flex-direction:column; justify-content:center; box-shadow:0 2px 12px var(--shadow); }}
  .p-edit-label {{ font-size:11px; color:var(--muted); margin-bottom:6px; }}
  .p-edit-input {{ width:100%; padding:7px 9px; border:1px solid var(--border); border-radius:8px; background:var(--input-bg); color:var(--text); font-size:13px; box-sizing:border-box; margin-bottom:8px; }}
  .p-edit-btns {{ display:flex; gap:8px; }}
  .p-edit-btns button {{ flex:1; padding:7px 0; border:none; border-radius:8px; font-size:12px; cursor:pointer; }}
  .p-edit-save {{ background:var(--accent); color:#fff; }}
  .p-edit-cancel {{ background:var(--card); color:var(--text); border:1px solid var(--border) !important; }}
  @media (max-width: 600px) {{ .providers-grid {{ grid-template-columns:1fr 1fr; }} }}
  /* 状态网格 — 聚合分散状态 (2列, 4张卡片含内核, 适配小窗口) */
  .status-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:12px; }}
  .status-card {{ background:var(--card); border-radius:12px; padding:14px 16px; box-shadow:0 1px 4px var(--shadow); }}
  .status-card h3 {{ font-size:13px; margin:0 0 8px; color:var(--muted); display:flex; align-items:center; gap:6px; }}
  .status-card .mini {{ font-size:12px; color:var(--muted); line-height:1.7; }}
  .status-card .mini b {{ color:var(--text); font-weight:500; }}
  .status-card .mini .row {{ padding:2px 0; border:none; font-size:12px; }}
  .status-card .mini .row .val {{ font-size:12px; }}
  /* 移动端网格变单列 */
  @media (max-width: 600px) {{
    .status-grid {{ grid-template-columns:1fr; }}
  }}
  h2 {{ font-size:15px; margin:16px 0 8px; color:var(--text); }}
  .status {{ display:inline-block; padding:4px 12px; border-radius:20px; font-size:13px; font-weight:600; }}
  .ok {{ background:var(--ok-bg); color:var(--ok-text); }}
  .down {{ background:var(--down-bg); color:var(--down-text); }}
  .row {{ display:flex; justify-content:space-between; align-items:flex-start; gap:12px; padding:8px 0; border-bottom:1px solid var(--border); font-size:14px; }}
  .row:last-child {{ border-bottom:none; }}
  .label {{ color:var(--muted); flex-shrink:0; }}
  .val {{ color:var(--text); font-family:monospace; word-break:break-all; text-align:right; }}
  .meta {{ color:var(--muted); font-size:12px; margin-top:16px; text-align:center; word-break:break-all; line-height:1.6; }}
  label {{ display:block; font-size:13px; color:var(--muted); margin:10px 0 4px; }}
  input {{ width:100%; padding:10px 12px; border:1px solid var(--border); border-radius:8px; font-size:16px; box-sizing:border-box; background:var(--input-bg); color:var(--text); }}
  input:focus {{ outline:none; border-color:var(--accent); box-shadow:0 0 0 2px rgba(47,111,237,.15); }}
  button {{ margin-top:16px; padding:12px 16px; border:none; border-radius:8px; font-size:15px; cursor:pointer; font-weight:500; }}
  .primary {{ background:var(--accent); color:#fff; width:100%; }}
  .save-btn {{ display:inline-block; margin-top:14px; padding:8px 20px; width:auto; font-size:13px; border:none; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; font-weight:500; }}
  .warn {{ background:var(--card); color:var(--text); border:1px solid var(--border); width:100%; }}
  button:active {{ opacity:.85; }}
  .msg {{ margin:12px 0; padding:10px; border-radius:8px; font-size:13px; display:none; word-break:break-all; }}
  .msg.ok {{ background:var(--ok-bg); color:var(--ok-text); display:block; }}
  .msg.err {{ background:var(--down-bg); color:var(--down-text); display:block; }}
  .btn-row {{ display:flex; gap:10px; margin-top:16px; }}
  .btn-row button {{ margin-top:0; flex:1; }}
  /* 移动端响应式 */
  @media (max-width: 480px) {{
    body {{ padding:10px; }}
    .card {{ padding:16px; border-radius:10px; margin-bottom:10px; }}
    h1 {{ font-size:18px; }}
    .row {{ font-size:14px; padding:7px 0; }}
    input {{ font-size:16px; padding:12px; }}  /* ≥16px 防 iOS 自动缩放 */
    button {{ font-size:15px; padding:14px; }}  /* 触控友好 */
    .btn-row {{ flex-direction:column; gap:8px; }}
    .meta {{ font-size:11px; }}
    .tab {{ padding:8px 14px; font-size:13px; }}
  }}
  @media (max-width: 320px) {{
    .row {{ flex-direction:column; gap:2px; }}
    .val {{ text-align:left; }}
  }}
</style>
<!-- xterm.js 终端 (本地打包, 避免 CDN 依赖) -->
<link rel="stylesheet" href="/vendor/xterm.min.css" />
<script src="/vendor/xterm.min.js"></script>
<script src="/vendor/xterm-addon-fit.min.js"></script>
</head>
<body data-theme="light">
  <div class="layout">
  <!-- 侧边栏导航 (参考 9Router) -->
  <div class="sidebar" id="sidebar">
    <div class="brand">🖥️ Hermes Core<div class="brand-ver" id="brandVer">v{APP_VERSION}</div></div>
    <div class="nav-item active" data-nav="chat" onclick="switchNav('chat')">
      <span class="ico">💬</span> <span data-i18n="nav-chat">聊天</span>
    </div>
    <div class="nav-item" data-nav="status" onclick="switchNav('status')">
      <span class="ico">📊</span> <span data-i18n="nav-status">状态</span>
    </div>
    <div class="nav-item" data-nav="config" onclick="switchNav('config')">
      <span class="ico">⚙️</span> <span data-i18n="nav-config">配置</span>
    </div>
    <div class="nav-item" data-nav="messaging" onclick="switchNav('messaging')">
      <span class="ico">📡</span> <span data-i18n="nav-messaging">消息平台</span>
    </div>
    <div class="nav-section" data-i18n="nav-providers">模型供应商</div>
    <div class="nav-item" data-nav="providers" onclick="switchNav('providers')">
      <span class="ico">🍟</span> <span data-i18n="nav-providers-title">供应商</span>
    </div>
    <div class="nav-item" data-nav="terminal" onclick="switchNav('terminal')">
      <span class="ico">🖥️</span> <span data-i18n="nav-terminal">终端</span>
    </div>
    <div class="nav-item" data-nav="logs" onclick="switchNav('logs')">
      <span class="ico">📜</span> <span data-i18n="nav-logs">日志</span>
    </div>
  </div>
  <div class="sidebar-overlay" id="sidebar-overlay" onclick="toggleSidebar()"></div>

  <!-- 主内容区 -->
  <div class="main">
  <div class="topbar">
    <div style="display:flex;align-items:center;gap:10px;">
      <button class="hamburger" onclick="toggleSidebar()">☰</button>
    </div>
    <div class="topbar-actions">
      <button class="icon-btn" onclick="toggleLang()" id="btn-lang" title="语言/Language">🌐</button>
      <button class="icon-btn" onclick="toggleTheme()" id="btn-theme">🌙</button>
      <div class="topbar-menu" id="topbar-menu">
        <button class="icon-btn" onclick="toggleTopMenu()" aria-label="菜单">⋮</button>
        <div class="menu-dropdown" id="menu-dropdown" style="display:none;">
          <div class="menu-item" onclick="openChangelog()"><span class="menu-ico">📜</span><span data-i18n="menu-changelog">更新日志</span></div>
          <div class="menu-item menu-danger" onclick="restartCore()"><span class="menu-ico">🔄</span><span data-i18n="restart">重启内核</span></div>
        </div>
      </div>
    </div>
  </div>

  <!-- 聊天面板 -->
  <div class="nav-panel" id="panel-chat">
  <div class="card chat-card">
    <h2>💬 <span data-i18n="nav-chat">聊天</span></h2>
    <div id="chat-msgs" class="chat-msgs"></div>
    <div class="chat-input-row">
      <textarea id="chat-input" class="chat-input" rows="2" placeholder="" data-i18n="chat-placeholder" enterkeyhint="send"></textarea>
      <button class="chat-send" onclick="sendChat()" aria-label="发送">↑</button>
    </div>
    <p style="font-size:11px;color:var(--muted);margin:6px 0 0;" data-i18n="chat-hint">通过本机 api_server (8642) 对话。发送即触发一次对话。</p>
  </div>
  </div>

  <!-- 终端面板 (真终端: PTY + WebSocket + xterm.js) -->
  <div class="nav-panel" id="panel-terminal" style="display:none">
  <div class="card">
    <h2>🖥️ <span data-i18n="nav-terminal">终端</span>
      <span style="font-size:11px;color:var(--muted);font-weight:normal;" data-i18n="terminal-hint">容器内终端 (shell)，可直接运行 hermes / 系统命令</span>
    </h2>
    <div id="term-container" style="height:calc(100vh - 220px);min-height:300px;background:#1e1e1e;border-radius:8px;padding:6px;"></div>
    <p style="font-size:11px;color:var(--muted);margin:6px 0 0;">
      <button class="mini-btn" onclick="termReconnect()">🔄 重连</button>
      <button class="mini-btn" onclick="termSendCtrlC()">⏹ Ctrl-C</button>
      <span id="term-status" style="margin-left:8px;">未连接</span>
    </p>
  </div>
  </div>

  <!-- 日志面板 (参考 hugo-blog 日志控制台) -->
  <div class="nav-panel" id="panel-logs" style="display:none">
  <div class="card">
    <h2>📜 <span data-i18n="console-title">日志控制台</span>
      <span style="font-size:11px;color:var(--muted);font-weight:normal;" data-i18n="console-hint">查看内核 / 状态页 / Dashboard / 安装日志</span>
    </h2>
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap;">
      <select id="log-source" onchange="loadLogs()" style="padding:6px 8px;border-radius:6px;border:1px solid var(--border);background:var(--card);color:var(--text);"></select>
      <button class="mini-btn" onclick="loadLogs()">🔄 <span data-i18n="refresh">刷新</span></button>
      <button class="mini-btn" onclick="downloadLog()">⬇️ <span data-i18n="download">下载</span></button>
      <span id="log-status" style="font-size:11px;color:var(--muted);"></span>
    </div>
    <pre id="log-view" style="background:#1e1e1e;color:#d4d4d4;padding:12px;border-radius:8px;height:calc(100vh - 220px);min-height:300px;overflow:auto;font-size:12px;line-height:1.5;white-space:pre-wrap;word-break:break-all;"></pre>
  </div>
  </div>

  <!-- 状态面板 -->
  <div class="nav-panel" id="panel-status" style="display:none">
  <!-- 聚合状态网格: 内核 / 消息网关 / LLM / Dashboard (2列4卡, 适配小窗口) -->
  <div class="status-grid">
    <div class="status-card">
      <h3>🔧 <span data-i18n="core-status">内核状态</span> <span class="status {STATUS_CLS}">{STATUS_TEXT}</span></h3>
      <div class="mini">
        <div class="row"><span class="label" data-i18n="state">状态</span><span class="val">{STATE}</span></div>
        <div class="row"><span class="label" data-i18n="platform">平台</span><span class="val">{PLATFORM}</span></div>
        <div class="row"><span class="label" data-i18n="version">版本</span><span class="val">{VERSION}</span></div>
        <div class="row"><span class="label" data-i18n="core-port">内核端口</span><span class="val">{CORE_PORT}</span></div>
        <div class="row"><span class="label" data-i18n="api-addr">API 地址</span><span class="val">http://127.0.0.1:{CORE_PORT}</span></div>
      </div>
    </div>
    <div class="status-card">
      <h3>📡 <span data-i18n="gateway">消息网关</span> <span class="status {GW_CLS}">{GW_TEXT}</span></h3>
      <div class="mini">{GW_PLATFORMS_MIN}</div>
    </div>
    <div class="status-card">
      <h3>🧠 <span data-i18n="fallback-llm">默认 LLM</span> <span class="status {LLM_CLS}">{LLM_TEXT}</span></h3>
      <div class="mini">{LLM_ROWS_MIN}</div>
    </div>
    <div class="status-card">
      <h3>📊 <span data-i18n="dashboard">Dashboard</span> <span class="status {DASH_CLS}">{DASH_TEXT}</span></h3>
      <div class="mini">
        <div><b data-i18n="state">状态</b>: {DASH_DETAIL}</div>
        <div><b data-i18n="user">用户</b>: {DASH_USER}</div>
        <div><b data-i18n="port">端口</b>: {DASH_PORT}</div>
      </div>
    </div>
  </div>
  </div>

  <!-- 配置面板 -->
  <div class="nav-panel" id="panel-config" style="display:none">
  <div class="card">
    <h2>⚙️ <span data-i18n="basic-config">基础配置</span></h2>
    <p style="font-size:12px;color:var(--muted);margin:0 0 8px;" data-i18n="config-hint">修改后点击保存，再点"重启内核"生效。敏感项已脱敏显示。</p>
    <div id="msg" class="msg"></div>
    <form id="cfgform">
      {FORM_FIELDS}
    </form>
    <div class="btn-row">
      <button class="save-btn" onclick="saveConfig('cfgform')" data-i18n="save-config">💾 保存配置</button>
    </div>
    <p style="font-size:12px;color:var(--muted);margin:12px 0 0;line-height:1.5;" data-i18n="restart-hint">
      ℹ️ 重启内核会同时重启 <b>消息网关</b>（Feishu/Telegram/微信等平台连接）与 cron 调度，消息平台短暂断开后自动恢复。重启请用右上角 🔄 按钮。
    </p>
  </div>
  </div>

  <!-- 消息平台面板 -->
  <div class="nav-panel" id="panel-messaging" style="display:none">
  <div class="card">
    <h2>📡 <span data-i18n="nav-messaging">消息平台</span></h2>
    <p style="font-size:12px;color:var(--muted);margin:0 0 12px;line-height:1.6;" data-i18n="messaging-hint">配置飞书/微信/QQ/钉钉消息渠道，让 Hermes 能从聊天平台收发消息。点击平台卡片展开配置，保存后点右上角 🔄 重启生效。</p>
    <div id="msg-messaging" class="msg"></div>
    <div class="msg-wrap">
      {MSG_FIELDS}
    </div>
  </div>
  </div>

  <!-- 模型供应商面板 -->
  <div class="nav-panel" id="panel-providers" style="display:none">
  <div class="card">
    <h2>🍟 <span data-i18n="nav-providers-title">供应商</span></h2>
    {DEFAULT_MODEL_HTML}
    <p style="font-size:12px;color:var(--muted);margin:12px 0 12px;" data-i18n="providers-hint">点击供应商卡片配置 API Key。默认模型由安装向导设置，9Router 为本地代理（非强制默认）。</p>
    {PROVIDERS_GRID}
  </div>
  </div>
  </div>
  </div>

<script>
const HERMES_AUTH = {AUTH_TOKEN};
const LLM_MODEL_NAME = {LLM_MODEL_JSON};
const I18N = {{
  zh: {{
    'nav-chat':'聊天','nav-status':'状态','nav-config':'配置','nav-messaging':'消息平台','nav-providers':'供应商','nav-providers-title':'供应商','local-kernel':'本地内核',
    'chat-placeholder':'输入消息，Enter 发送...','chat-hint':'通过本机 api_server (8642) 对话。发送即触发一次对话。',
    'providers-hint':'点击供应商卡片配置 API Key。默认模型由安装向导设置，9Router 为本地代理（非强制默认）。',
    'messaging-hint':'配置飞书/微信/QQ/钉钉消息渠道，让 Hermes 能从聊天平台收发消息。点击平台卡片展开配置，保存后点右上角 🔄 重启生效。',
    'messaging-status':'📡 状态提示：配置飞书/微信后重启内核，Hermes 消息网关即连接对应平台。当前渠道连接状态见「状态」页的消息网关卡片。',
    'menu-lang':'语言切换','menu-theme':'主题','menu-changelog':'更新日志',
    'wxqr-hint':'用微信扫二维码即可自动绑定账号并写入 Token，无需手动填账号 ID/Token。扫码需联网访问微信 iLink。',
    'wxqr-start':'📱 开始扫码登录','wxqr-wait':'用微信扫一扫上面的二维码...','wxqr-open':'打不开？点这里打开二维码链接',
    'feishu-hint':'配置飞书消息渠道，保存后重启内核生效。验证 Token 为飞书开放平台下发的验证凭据。',
    'wechat-hint':'配置微信消息渠道，保存后重启内核生效。Token 为微信渠道下发的验证凭据。',
    'core-status':'内核状态','state':'状态','platform':'平台',
    'version':'版本','core-port':'内核端口','api-addr':'API 地址','gateway':'消息网关','fallback-llm':'默认 LLM',
    'dashboard':'Dashboard','user':'用户','port':'端口','basic-config':'基础配置','config-hint':'修改后点击保存，再点"重启内核"生效。敏感项已脱敏显示。',
    'save-config':'💾 保存配置','restart':'重启内核','restart-hint':'ℹ️ 重启内核会同时重启 消息网关 与 cron 调度',
    'saved':'✅ 配置已保存，请点"重启内核"生效','save-fail':'❌ 保存失败: ','restarting':'🔄 内核正在重启，几秒后刷新页面查看状态','restart-fail':'❌ 重启失败: ',
    'running':'● 运行中','stopped':'● 已停止','healthy':'healthy','unconfigured':'○ 未配置',
    'nav-logs':'日志','console-title':'日志控制台','console-hint':'查看内核 / 状态页 / Dashboard / 安装日志','refresh':'刷新','download':'下载'
  }},
  en: {{
    'nav-chat':'Chat','nav-status':'Status','nav-config':'Config','nav-messaging':'Messaging','nav-providers':'Providers','nav-providers-title':'Providers','local-kernel':'Local Kernel',
    'chat-placeholder':'Type a message, Enter to send...','chat-hint':'Chat via local api_server (8642). Sending triggers one conversation.',
    'providers-hint':'Click a provider card to configure its API Key. Default model is set in install wizard; 9Router is a local proxy (not forced default).',
    'messaging-hint':'Configure Feishu/WeChat/QQ/DingTalk messaging channels so Hermes can send/receive messages from chat platforms. Click a platform card to expand config, save then restart with the top-right 🔄.',
    'messaging-status':'📡 Tip: after configuring Feishu/WeChat and restarting the core, the Hermes message gateway connects to those platforms. See the Message Gateway card on the Status page for current connection state.',
    'menu-lang':'Language','menu-theme':'Theme','menu-changelog':'Changelog',
    'wxqr-hint':'Scan the QR with WeChat to auto-bind your account and write the token — no need to fill Account ID/Token manually. Requires internet access to WeChat iLink.',
    'wxqr-start':'📱 Start QR Login','wxqr-wait':'Scan the QR code above with WeChat...','wxqr-open':'Cannot open? Click here for the QR link',
    'feishu-hint':'Configure Feishu channel. Save and restart to apply. Verification Token comes from Feishu Open Platform.',
    'wechat-hint':'Configure WeChat channel. Save and restart to apply. Token comes from WeChat channel.',
    'core-status':'Core Status','state':'State','platform':'Platform',
    'version':'Version','core-port':'Core Port','api-addr':'API Address','gateway':'Message Gateway','fallback-llm':'Default LLM',
    'dashboard':'Dashboard','user':'User','port':'Port','basic-config':'Basic Config','config-hint':'Edit then click Save, then Restart Core to apply. Sensitive fields are masked.',
    'save-config':'💾 Save Config','restart':'Restart Core','restart-hint':'ℹ️ Restarting the core also restarts the message gateway and cron scheduler',
    'saved':'✅ Config saved, click "Restart Core" to apply','save-fail':'❌ Save failed: ','restarting':'🔄 Core restarting, refresh in a few seconds','restart-fail':'❌ Restart failed: ',
    'running':'● Running','stopped':'● Stopped','healthy':'healthy','unconfigured':'○ Not configured',
    'nav-logs':'Logs','console-title':'Log Console','console-hint':'View core / status / dashboard / install logs','refresh':'Refresh','download':'Download'
  }},
}};
// 安全 localStorage (移动端 WebView/隐私模式可能无持久化缓存, 直接访问会抛异常导致整段脚本中断)
function lsGet(k) {{ try {{ return window.localStorage.getItem(k); }} catch (e) {{ return null; }} }}
function lsSet(k, v) {{ try {{ window.localStorage.setItem(k, v); }} catch (e) {{ /* 忽略 */ }} }}
let currentLang = lsGet('hermes_lang') || 'zh';
let currentTheme = lsGet('hermes_theme') || 'light';

function applyI18n() {{
  document.querySelectorAll('[data-i18n]').forEach(el => {{
    const key = el.dataset.i18n;
    const val = I18N[currentLang][key];
    if (!val) return;
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {{ el.placeholder = val; }}
    else {{ el.textContent = val; }}
  }});
  document.getElementById('btn-lang').textContent = '🌐';
  // 更新动态生成的表单 label/状态 (通过 data-i18n 无法覆盖, 用替换文本)
  document.querySelectorAll('label').forEach(l => {{
    // label 文本由后端生成, i18n 主要覆盖静态部分
  }});
}}
function toggleLang() {{
  currentLang = currentLang === 'zh' ? 'en' : 'zh';
  lsSet('hermes_lang', currentLang);
  applyI18n();
}}
function toggleTheme() {{
  currentTheme = currentTheme === 'light' ? 'dark' : 'light';
  lsSet('hermes_theme', currentTheme);
  document.body.dataset.theme = currentTheme;
  const b = document.getElementById('btn-theme');
  if (b) b.textContent = currentTheme === 'light' ? '🌙' : '☀️';
  const dd = document.getElementById('menu-dropdown');
  if (dd) dd.style.display = 'none';
}}
function toggleTopMenu() {{
  const dd = document.getElementById('menu-dropdown');
  if (!dd) return;
  dd.style.display = dd.style.display === 'none' ? 'block' : 'none';
}}
function openChangelog() {{
  const dd = document.getElementById('menu-dropdown');
  if (dd) dd.style.display = 'none';
  // 打开 GitHub Releases 页 (跳转到更新日志)
  window.open('https://github.com/techysy/hermes-core-fnos/releases', '_blank');
}}
// 点击空白处关闭下拉菜单
document.addEventListener('click', (e) => {{
  const tm = document.getElementById('topbar-menu');
  if (tm && !tm.contains(e.target)) {{
    const dd = document.getElementById('menu-dropdown');
    if (dd) dd.style.display = 'none';
  }}
}});
function switchNav(nav) {{
  // 切换侧边栏菜单
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.nav === nav));
  document.querySelectorAll('.nav-panel').forEach(p => p.style.display = (p.id === 'panel-' + nav) ? 'block' : 'none');
  // 切到日志页时加载日志源
  if (nav === 'logs') loadLogSources();
  // 切到终端页时, 面板已可见, 重新 fit 并同步 PTY 尺寸 (修复隐藏时 fit 成 1 列导致单字符换行)
  if (nav === 'terminal') {{ setTimeout(sendTermSize, 50); }}
  // 移动端: 切换后收起侧栏
  if (window.innerWidth <= 768) toggleSidebar(false);
}}
// ── 日志控制台 (参考 hugo-blog) ─────────────────────────────
async function loadLogSources() {{
  const sel = document.getElementById('log-source');
  if (!sel) return;
  const st = document.getElementById('log-status');
  try {{
    const r = await api('/api/logs/list', 'GET');
    if (!r || !r.ok) {{ if (st) st.textContent = '加载失败'; return; }}
    sel.innerHTML = (r.sources || []).map(function(s){{ return '<option value="' + s.key + '">' + s.label + '</option>'; }}).join('');
    loadLogs();
  }} catch(e) {{ if (st) st.textContent = '加载失败'; }}
}}
async function loadLogs() {{
  const sel = document.getElementById('log-source');
  const view = document.getElementById('log-view');
  const st = document.getElementById('log-status');
  if (!sel || !view) return;
  const src = sel.value || 'core';
  try {{
    if (st) st.textContent = '加载中...';
    const r = await api('/api/logs?source=' + encodeURIComponent(src) + '&tail=300', 'GET');
    if (!r || !r.ok) {{ view.textContent = '加载失败'; if (st) st.textContent=''; return; }}
    view.textContent = r.content || '(空)';
    view.scrollTop = view.scrollHeight;
    if (st) st.textContent = r.source + ' · ' + (r.content||'').split(String.fromCharCode(10)).length + ' 行';
  }} catch(e) {{ view.textContent = '加载失败'; if (st) st.textContent=''; }}
}}
function downloadLog() {{
  const sel = document.getElementById('log-source');
  const src = (sel && sel.value) || 'core';
  window.open('/api/logs/download?source=' + encodeURIComponent(src), '_blank');
}}
// 消息平台卡片: 点击打开该平台的配置覆盖层
function openMsgCard(grp) {{
  document.querySelectorAll('.msg-edit').forEach(e => e.style.display = 'none');
  const ov = document.getElementById('msg-edit-' + grp);
  if (ov) ov.style.display = 'block';
}}
function closeMsgCard(grp) {{
  const ov = document.getElementById('msg-edit-' + grp);
  if (ov) ov.style.display = 'none';
}}
// 消息平台卡片: 保存该平台覆盖层内的字段, 保存成功后自动重启内核生效
async function saveMsgCard(grp) {{
  const ov = document.getElementById('msg-edit-' + grp);
  if (!ov) return;
  const sensitive = ['FEISHU_APP_SECRET','FEISHU_VERIFICATION_TOKEN','FEISHU_ENCRYPT_KEY',
                     'WEIXIN_TOKEN','QQ_APP_SECRET','DINGTALK_CLIENT_SECRET'];
  const data = {{}};
  ov.querySelectorAll('input').forEach(inp => {{
    const k = inp.name, v = inp.value.trim();
    if (sensitive.includes(k) && !v) return; // 敏感留空 = 不修改
    if (!k) return;
    data[k] = v;
  }});
  const r = await api('/api/config', 'POST', data);
  if (r.ok) {{
    closeMsgCard(grp);
    showMsg('✅ 已保存，正在重启内核生效...', false, 'msg-messaging');
    restartCore();  // 自动重启 (原: 仅提示手动点重启 + location.reload)
  }} else {{
    showMsg('❌ 保存失败: ' + (r.error || ''), true, 'msg-messaging');
  }}
}}
function toggleSidebar(open) {{
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('sidebar-overlay');
  const isOpen = open === undefined ? !sidebar.classList.contains('open') : open;
  sidebar.classList.toggle('open', isOpen);
  overlay.classList.toggle('show', isOpen);
}}
const PROVIDER_ENV = {{ '9router':'ROUTER_API_KEY', 'deepseek':'DEEPSEEK_API_KEY', 'mimo':'XIAOMI_API_KEY' }};
const PROVIDER_NAME = {{ '9router':'9Router','deepseek':'DeepSeek','mimo':'Xiaomi MiMo' }};
function editProvider(key) {{
  // 收起其他卡片的展开区
  document.querySelectorAll('.p-edit').forEach(e => e.remove());
  const card = document.querySelector('.provider-card[data-provider="' + key + '"]');
  if (!card) return;
  // 在卡片内插入内联配置区 (响应式)
  const name = PROVIDER_NAME[key] || key;
  const edit = document.createElement('div');
  edit.className = 'p-edit';
  // 用 DOM API 构建, 避免内联 onclick 的引号转义问题
  const label = document.createElement('div');
  label.className = 'p-edit-label';
  label.textContent = name + ' API Key (留空则不改)';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'p-edit-input';
  input.placeholder = '输入 API Key...';
  input.autocomplete = 'off';
  const btns = document.createElement('div');
  btns.className = 'p-edit-btns';
  const saveBtn = document.createElement('button');
  saveBtn.className = 'p-edit-save';
  saveBtn.textContent = '保存';
  saveBtn.onclick = (e) => {{ e.stopPropagation(); saveProvider(key); }};
  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'p-edit-cancel';
  cancelBtn.textContent = '取消';
  cancelBtn.onclick = (e) => {{ e.stopPropagation(); edit.remove(); }};
  btns.appendChild(saveBtn);
  btns.appendChild(cancelBtn);
  edit.appendChild(label);
  edit.appendChild(input);
  edit.appendChild(btns);
  // 阻止点击编辑区冒泡到卡片 toggle, 避免重新展开导致输入/按钮被重置
  edit.onclick = (e) => e.stopPropagation();
  card.appendChild(edit);
  input.focus();
  input.addEventListener('keydown', (e) => {{ if (e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); e.stopPropagation(); saveProvider(key); }} }});
}}
// 点击空白处退出 token 填写状态
document.addEventListener('click', (e) => {{
  if (e.target.closest('.provider-card') || e.target.closest('.p-edit')) return;
  document.querySelectorAll('.p-edit').forEach(el => el.remove());
}});
async function saveProvider(key) {{
  const env = PROVIDER_ENV[key] || '';
  const edit = document.querySelector('.provider-card[data-provider="' + key + '"] .p-edit');
  const input = edit ? edit.querySelector('input') : null;
  const val = input ? input.value.trim() : '';
  const data = {{}};
  data[env] = val;
  const r = await api('/api/config', 'POST', data);
  if (r.ok) {{ setTimeout(() => location.reload(), 600); }}
  else {{ alert('保存失败: ' + (r.error || '')); }}
}}

async function api(path, method, body) {{
  const headers = {{ 'Content-Type': 'application/json' }};
  if (HERMES_AUTH) headers['Authorization'] = 'Bearer ' + HERMES_AUTH;
  const res = await fetch(path, {{
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined
  }});
  const data = await res.json().catch(() => ({{}}));
  return {{ ok: res.ok, ...data }};
}}
function showMsg(text, isErr, elId) {{
  const el = document.getElementById(elId || 'msg');
  el.textContent = text;
  el.className = 'msg ' + (isErr ? 'err' : 'ok');
}}
async function saveConfig(formId, msgId) {{
  const form = document.getElementById(formId || 'cfgform');
  const fd = new FormData(form);
  // 敏感字段留空 = 不修改 (保留原值)
  const sensitive = ['API_SERVER_KEY', 'ROUTER_API_KEY', 'LLM_API_KEY', 'DASHBOARD_PASSWORD',
                     'FEISHU_APP_SECRET', 'FEISHU_VERIFICATION_TOKEN', 'FEISHU_ENCRYPT_KEY', 'WEIXIN_TOKEN'];
  const data = Object.fromEntries(
    [...fd.entries()].filter(([k, v]) => !(sensitive.includes(k) && !v.trim()))
  );
  const r = await api('/api/config', 'POST', data);
  if (r.ok) {{
    showMsg(I18N[currentLang]['saved'], false, msgId);
    restartCore();  // 自动重启生效 (原: 仅提示手动点重启)
  }}
  else showMsg(I18N[currentLang]['save-fail'] + (r.error || ''), true, msgId);
}}
async function restartCore() {{
  const r = await api('/api/restart', 'POST', {{}});
  if (r.ok) {{
    // 状态服务会被重启过程杀掉, 立即显示"重启中"并自动刷新, 避免按钮看起来"无效"
    showMsg(I18N[currentLang]['restarting']);
    setTimeout(() => location.reload(), 5000);
  }} else {{
    showMsg(I18N[currentLang]['restart-fail'] + (r.error || ''), true);
  }}
}}
// 微信扫码登录: 获取二维码 → 显示 → 轮询状态 → confirmed 自动写 gateway.env
let wxQrTimer = null;
async function wxQrStart() {{
  const area = document.getElementById('wxqr-area');
  const msgEl = document.getElementById('wxqr-msg');
  if (wxQrTimer) {{ clearInterval(wxQrTimer); wxQrTimer = null; }}
  const r = await api('/api/weixin/qr/start', 'POST', {{}});
  if (!r.ok) {{
    showMsg('❌ ' + (r.error || '二维码获取失败'), true, 'msg-messaging');
    return;
  }}
  area.style.display = 'block';
  const qrUrl = encodeURIComponent(r.qrcode_url || r.qrcode_value);
  // 用在线 QR 渲染服务生成二维码图 (二维码内容是 liteapp URL, 需微信扫)
  document.getElementById('wxqr-img').src = 'https://api.qrserver.com/v1/create-qr-code/?size=200x200&data=' + qrUrl;
  document.getElementById('wxqr-link').href = r.qrcode_url || r.qrcode_value;
  msgEl.textContent = '用微信扫一扫上面的二维码...';
  // 每 2 秒轮询状态
  wxQrTimer = setInterval(async () => {{
    const s = await api('/api/weixin/qr/status?qrcode=' + encodeURIComponent(r.qrcode_value), 'GET');
    if (s.status === 'confirmed') {{
      clearInterval(wxQrTimer); wxQrTimer = null;
      msgEl.textContent = '✅ 微信已连接！account_id=' + (s.account_id || '');
      showMsg('✅ 微信扫码成功，账号已写入 gateway.env。点「重启内核」生效。', false, 'msg-messaging');
    }} else if (s.status === 'scaned') {{
      msgEl.textContent = '已扫码，请在微信里确认...';
    }} else if (s.status === 'expired') {{
      msgEl.textContent = '二维码已过期，请点「开始扫码登录」刷新';
    }} else if (!s.ok) {{
      msgEl.textContent = '⚠ ' + (s.error || '轮询失败');
    }}
  }}, 2000);
}}
let chatHistory = [];
function addChatMsg(role, text) {{
  const box = document.getElementById('chat-msgs');
  const div = document.createElement('div');
  div.className = 'chat-msg ' + (role === 'user' ? 'user' : (role === 'error' ? 'error' : 'assistant'));
  if (role !== 'user') {{
    const r = document.createElement('div');
    r.className = 'role';
    r.textContent = role === 'assistant' ? 'Hermes' : '错误';
    div.appendChild(r);
  }}
  const body = document.createElement('span');
  body.className = 'msg-body';
  body.textContent = text;
  div.appendChild(body);
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return {{ el: div, body: body, box: box }};
}}
function boxScrollBottom() {{
  const box = document.getElementById('chat-msgs');
  if (box) box.scrollTop = box.scrollHeight;
}}
async function sendChat() {{
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  addChatMsg('user', text);
  chatHistory.push({{ role: 'user', content: text }});
  // 显示思考中占位
  const think = addChatMsg('assistant', '⏳ 思考中...');
  const startTime = Date.now();
  const headers = {{ 'Content-Type': 'application/json' }};
  if (HERMES_AUTH) headers['Authorization'] = 'Bearer ' + HERMES_AUTH;
  let replyText = '';
  let modelName = '';
  try {{
    const res = await fetch('/api/chat', {{
      method: 'POST',
      headers,
      body: JSON.stringify({{ messages: chatHistory, stream: true }})
    }});
    if (!res.ok) {{
      const d = await res.json().catch(() => ({{}}));
      throw new Error(d.error || ('HTTP ' + res.status));
    }}
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let curEvent = '';
    while (true) {{
      const {{ done, value }} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {{ stream: true }});
      // 按行解析 SSE
      let idx;
      while ((idx = buf.indexOf('\\n\\n')) !== -1) {{
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const lines = chunk.split('\\n');
        for (const line of lines) {{
          if (line.startsWith('event:')) {{ curEvent = line.slice(6).trim(); continue; }}
          if (line.startsWith('data:')) {{
            const payload = line.slice(5).trim();
            if (payload === '[DONE]') {{ break; }}
            try {{
              const obj = JSON.parse(payload);
              if (curEvent === 'meta') {{ if (obj.model) modelName = obj.model; }}
              else if (obj.text) {{ replyText += obj.text; think.body.textContent = replyText; boxScrollBottom(); }}
            }} catch (e) {{}}
          }}
        }}
        curEvent = '';
      }}
    }}
  }} catch (err) {{
    think.el.className = 'chat-msg error';
    think.body.textContent = '对话失败: ' + err.message;
  }}
  if (replyText) {{
    // 计算耗时 (对齐飞书流式卡片 _format_elapsed: <60s 用 1 位小数, >=60s 用 Xm Ys)
    const ms = Date.now() - startTime;
    const seconds = ms / 1000;
    const dur = seconds < 60 ? (seconds.toFixed(1) + 's') : (Math.floor(seconds / 60) + 'm ' + Math.floor(seconds % 60) + 's');
    think.body.textContent = replyText;
    // 底部元信息: [ 耗时 · 模型 ] — 独立小号元素, 与正文隔断
    const mname = modelName || LLM_MODEL_NAME;
    if (dur) {{
      const metaDiv = document.createElement('div');
      metaDiv.className = 'msg-meta';
      metaDiv.textContent = '[ ' + dur + (mname ? ' · ' + mname : '') + ' ]';
      think.el.appendChild(metaDiv);
    }}
    chatHistory.push({{ role: 'assistant', content: replyText }});
  }}
}}
function toggleDashEnable() {{
  const hid = document.getElementById('dash-enable-val');
  const btn = document.getElementById('dash-enable-btn');
  const txt = document.querySelector('.switch-text');
  const isOn = (hid.value || '').toLowerCase() === 'true';
  const next = isOn ? 'false' : 'true';
  hid.value = next;
  btn.classList.toggle('on', next === 'true');
  txt.textContent = next === 'true' ? '开启' : '关闭';
}}
// 供应商默认 base_url (与后端 MODEL_PROVIDER_URLS 一致)
const DM_URLS = {{ '9router':'http://127.0.0.1:20128/v1', 'deepseek':'https://api.deepseek.com/v1', 'mimo':'https://api.xiaomimimo.com/v1' }};
const DM_DEF_MODEL = {{ '9router':'', 'deepseek':'deepseek-chat', 'mimo':'mimo-v2.5' }};
// 选择供应商时自动带出 Base URL 和默认模型名 (兼容移动端: 不依赖 localStorage)
function dmProviderChanged() {{
  const prov = document.getElementById('dm-provider').value;
  const baseInput = document.getElementById('dm-base');
  const modelInput = document.getElementById('dm-model');
  if (prov === 'custom') {{
    // 自定义: 清空 base, 保留用户已填的模型名
    baseInput.value = '';
    return;
  }}
  if (DM_URLS[prov]) baseInput.value = DM_URLS[prov];
  // 仅在模型名输入框为空时才带出默认模型名
  if (!modelInput.value.trim() && DM_DEF_MODEL[prov]) modelInput.value = DM_DEF_MODEL[prov];
}}
async function saveDefaultModel() {{
  const prov = document.getElementById('dm-provider').value;
  let model = document.getElementById('dm-model').value.trim();
  let base = document.getElementById('dm-base').value.trim();
  if (prov !== 'custom' && !base) base = DM_URLS[prov] || '';
  if (!model && DM_DEF_MODEL[prov]) model = DM_DEF_MODEL[prov];
  const data = {{ LLM_MODEL: model, LLM_BASE_URL: base }};
  const r = await api('/api/config', 'POST', data);
  if (r.ok) showMsg('✅ 默认模型已保存，请点「重启内核」生效');
  else showMsg('❌ 保存失败: ' + (r.error || ''), true);
}}
document.addEventListener('keydown', (e) => {{
  const input = e.target && e.target.id === 'chat-input' ? e.target : null;
  if (input && e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); sendChat(); }}
}});
// 初始化
document.body.dataset.theme = currentTheme;
document.getElementById('btn-theme').textContent = currentTheme === 'light' ? '🌙' : '☀️';
applyI18n();
// 移动端默认收起侧栏, 避免挤压主内容
if (window.innerWidth <= 768) toggleSidebar(false);
// ── 终端 (真终端: xterm.js + WebSocket + PTY) ─────────────────────────
let term = null, termFit = null, termWs = null, termReady = false;

function termUrl() {{
  // 相对路径: 官方网关 /app/hermescore/ 下 → /app/hermescore/api/pty; 根路径 → /api/pty
  const proto = (window.location.protocol === 'https:' || window.location.protocol === 'wss:') ? 'wss:' : 'ws:';
  let base = window.location.pathname;
  if (!base.endsWith('/')) base = base.substring(0, base.lastIndexOf('/') + 1);
  // 去掉双斜杠: 根路径 "/" 时 base+"/api/pty" 会得到 "//api/pty"
  let path = (base + 'api/pty').replace(/\/{{2,}}/g, '/');
  return proto + '//' + window.location.host + path;
}}

function initTerm() {{
  const el = document.getElementById('term-container');
  if (!el || typeof Terminal === 'undefined') return;
  if (term) {{ term.dispose(); }}
  term = new Terminal({{
    cursorBlink: true,
    fontSize: 13,
    theme: {{ background: '#1e1e1e', foreground: '#d4d4d4' }},
    scrollback: 5000,
  }});
  termFit = new FitAddon.FitAddon();
  term.loadAddon(termFit);
  term.open(el);
  termFit.fit();
  term.onData((d) => {{ if (termWs && termWs.readyState === 1) termWs.send(d); }});
  term.onResize((s) => {{ if (termWs && termWs.readyState === 1) termWs.send(String.fromCharCode(27) + '[' + s.rows + ';' + s.cols + 'R'); }});
  window.addEventListener('resize', () => {{ if (termFit) try {{ termFit.fit(); }} catch(e){{}} }});
  connectTerm();
}}

function connectTerm() {{
  const st = document.getElementById('term-status');
  try {{ termWs = new WebSocket(termUrl()); }}
  catch (e) {{ if (st) st.textContent = '连接失败'; return; }}
  if (st) st.textContent = '连接中...';
  termWs.binaryType = 'arraybuffer';
  termWs.onopen = () => {{ if (st) st.textContent = '已连接 (容器终端)'; termReady = true; term.focus(); sendTermSize(); }};
  termWs.onmessage = (ev) => {{
    let data = ev.data;
    if (typeof data === 'string') data = data;
    else data = new Uint8Array(data);
    term.write(data);
  }};
  termWs.onclose = () => {{ if (st) st.textContent = '已断开 (点 🔄 重连)'; termReady = false; }};
  termWs.onerror = () => {{ if (st) st.textContent = '错误 (点 🔄 重连)'; }};
}}
// 连接后/窗口变化时同步终端尺寸 (让 PTY 匹配前端实际列数, 避免多行格式错乱)
function sendTermSize() {{
  if (!termFit || !termWs || termWs.readyState !== 1) return;
  try {{
    termFit.fit();
    const p = term.proposeDimensions();
    let cols = p ? p.cols : 80, rows = p ? p.rows : 24;
    // 面板隐藏/异常时 fit 会得到极小列数, 用默认 80x24 兜底, 避免 PTY 单字符换行
    if (!cols || cols < 10 || !rows || rows < 3) {{ cols = 80; rows = 24; }}
    termWs.send(String.fromCharCode(27) + '[' + rows + ';' + cols + 'R');
  }} catch(e) {{}}
}}

function termReconnect() {{ if (termWs) {{ try {{ termWs.close(); }} catch(e){{}} }} setTimeout(connectTerm, 300); }}
function termSendCtrlC() {{ if (termWs && termWs.readyState === 1) termWs.send('\\x03'); }}
// 进入终端 tab 时初始化
document.addEventListener('DOMContentLoaded', () => {{ initTerm(); loadBrandVersion(); }});
// ── 品牌区版本号 (参考 Strava/hugo: 通过 /api/version 动态填充 brandVer) ──
async function loadBrandVersion() {{
  try {{
    const r = await fetch('/api/version');
    const d = await r.json();
    if (d && d.version) {{ const bv = document.getElementById('brandVer'); if (bv) bv.textContent = 'v' + d.version; }}
  }} catch(e) {{}}
}}
</script>
</body>
</html>
"""


def _render_group_fields(cfg, grp_key):
    """渲染单个分组为独立区块卡片."""
    grp_title, _ = CONFIG_GROUPS.get(grp_key, (grp_key, ""))
    fields_html = []
    # 内核分组: 监听地址 + API 端口 放同一排 (flex row)
    if grp_key == "core":
        row = []
        for key, label, sensitive, grp in CONFIG_FIELDS:
            if grp != grp_key or key in ("API_SERVER_KEY",):
                continue
            val = cfg.get(key, "")
            row.append(
                f'<div class="field-col"><label>{label}</label>'
                f'<input type="text" name="{key}" value="{val}" autocomplete="off"></div>'
            )
        if row:
            fields_html.append('<div class="field-row">' + "".join(row) + "</div>")
    for key, label, sensitive, grp in CONFIG_FIELDS:
        if grp != grp_key:
            continue
        # Dashboard 开关 → 按钮 (switch) — 与 cmd/main 一致: 未配置 = 启用
        if key == "DASHBOARD_ENABLED":
            val = cfg.get(key, "true")
            checked = ("true" in (val or "").lower()) or not (val or "").strip()
            fields_html.append(f'<label>{label}</label>')
            fields_html.append(
                f'<input type="hidden" id="dash-enable-val" name="DASHBOARD_ENABLED" value="{val}">'
                f'<div class="switch-wrap">'
                f'<button type="button" id="dash-enable-btn" class="switch{" on" if checked else ""}" '
                f'onclick="toggleDashEnable()"><span class="knob"></span></button>'
                f'<span class="switch-text">{ "开启" if checked else "关闭" }</span></div>'
            )
            continue
        if key == "API_SERVER_KEY":
            shown = _mask(val) if val else "未设置"
            ph = f"当前值: {shown}（留空则不改）"
            fields_html.append(f'<label>{label}</label>')
            fields_html.append(f'<input type="text" name="{key}" placeholder="{ph}" value="" autocomplete="off">')
            continue
        if grp_key == "core":
            continue  # 已在上面 flex row 渲染
        val = cfg.get(key, "")
        if sensitive:
            shown = _mask(val) if val else "未设置"
            ph = f"当前值: {shown}（留空则不改）"
            fields_html.append(f'<label>{label}</label>')
            fields_html.append(f'<input type="text" name="{key}" placeholder="{ph}" value="" autocomplete="off">')
        else:
            # 代理分组: 输入框留空时显示默认值提示 (默认走本机 mihomo, 与 cmd/main 导出一致)
            # HTTP/HTTPS 代理一般不分: HTTPS_PROXY 留空时跟随 HTTP_PROXY (cmd/main 兜底)
            ph = ""
            if grp_key == "proxy":
                if key == "HTTPS_PROXY":
                    ph = "留空则跟随 HTTP 代理"
                else:
                    _dflt = {"HTTP_PROXY": "http://127.0.0.1:7890",
                             "NO_PROXY": "localhost,127.0.0.1,192.168.*"}.get(key, "")
                    ph = f"默认: {_dflt}（留空用默认）" if _dflt else ""
            fields_html.append(f'<label>{label}</label>')
            fields_html.append(f'<input type="text" name="{key}" value="{val}" placeholder="{ph}" autocomplete="off">')
    if not fields_html:
        return ""
    return (f'<div class="cfg-section">'
            f'<div class="cfg-section-title">{grp_title}</div>'
            f'{"".join(fields_html)}'
            f'</div>')


def _form_fields(cfg):
    """配置面板: 内核/Dashboard/代理 分组 (LLM 配置走安装向导+模型供应商页, 不再显示)."""
    parts = []
    for grp_key in ("core", "dash", "proxy"):
        body = _render_group_fields(cfg, grp_key)
        if body:
            parts.append(body)
    return "\n".join(parts)


def _form_fields_feishu(cfg):
    """飞书面板字段."""
    return _render_group_fields(cfg, "feishu")


def _form_fields_wechat(cfg):
    """微信面板字段."""
    return _render_group_fields(cfg, "wechat")


def _msg_fields(cfg):
    """消息平台面板: 平台卡片网格. 点击卡片 → 绝对定位覆盖层编辑配置 (同供应商卡片交互, 不撑开卡片)."""
    platforms = [
        {"grp": "feishu", "name": "飞书", "ico": "💬", "bg": "#3370ff", "desc": "企业自建应用，支持扫码授权"},
        {"grp": "wechat", "name": "微信", "ico": "💚", "bg": "#07c160", "desc": "im.bot 通道，支持扫码登录"},
        {"grp": "qq", "name": "QQ", "ico": "🐧", "bg": "#12b7f5", "desc": "QQ 机器人 App ID/Secret"},
        {"grp": "dingtalk", "name": "钉钉", "ico": "📌", "bg": "#0089ff", "desc": "钉钉机器人 Client ID/Secret"},
    ]
    cards = []
    for p in platforms:
        grp = p["grp"]
        # 生成该平台的字段覆盖层 (绝对定位, 不撑开卡片)
        fields = _render_msg_overlay(cfg, grp, p)
        keys = [k for k, _l, _s, g in CONFIG_FIELDS if g == grp]
        configured = any(cfg.get(k, "") for k in keys)
        status_txt = "🟢 已配置" if configured else "⚪ 未配置"
        cards.append(
            f'<div class="msg-card" data-msg="{grp}" onclick="openMsgCard(\'{grp}\')">'
            f'<div class="msg-card-ico" style="background:{p["bg"]};">{p["ico"]}</div>'
            f'<div class="msg-card-name">{p["name"]}</div>'
            f'<span class="msg-card-status">{status_txt}</span>'
            f'<div class="msg-card-desc">{p["desc"]}</div>'
            f'{fields}'
            f'</div>'
        )
    return '<div class="msg-grid">' + "".join(cards) + "</div>"


def _render_msg_overlay(cfg, grp, p):
    """渲染单个平台的配置覆盖层 (绝对定位). 含扫码入口 + 字段 + 保存/取消."""
    # 该平台的可配置字段
    pfields = [f for f in CONFIG_FIELDS if f[3] == grp]
    if not pfields:
        return ""
    inputs = []
    for key, label, sensitive, _g in pfields:
        val = cfg.get(key, "")
        if sensitive:
            shown = _mask(val) if val else "未设置"
            ph = f"当前: {shown}（留空不改）"
            inputs.append(f'<label>{label}</label><input type="text" name="{key}" placeholder="{ph}" value="" autocomplete="off">')
        else:
            inputs.append(f'<label>{label}</label><input type="text" name="{key}" value="{val}" autocomplete="off">')
    # 扫码入口: 微信用 iLink 扫码; 飞书用授权链接占位 (如有)
    qr_btn = ""
    if grp == "wechat":
        qr_btn = (f'<div style="margin:6px 0 2px;">'
                  f'<button type="button" class="msg-qr-btn" onclick="wxQrStart()">📱 扫码登录</button>'
                  f'<div id="wxqr-area" style="display:none;margin-top:8px;text-align:center;">'
                  f'<img id="wxqr-img" style="width:150px;height:150px;border:1px solid var(--border);border-radius:8px;background:#fff;" alt="微信二维码"/>'
                  f'<div id="wxqr-msg" style="font-size:11px;color:var(--muted);margin-top:6px;">用微信扫一扫上面的二维码...</div>'
                  f'<div><a id="wxqr-link" href="#" target="_blank" style="font-size:10px;color:var(--accent);">打不开？点这里打开链接</a></div>'
                  f'</div></div>')
    elif grp == "feishu":
        qr_btn = (f'<div style="margin:6px 0 2px;">'
                  f'<a href="https://open.feishu.cn/app?lang=zh-CN" target="_blank" class="msg-qr-btn" style="display:inline-block;text-decoration:none;text-align:center;">🚀 去飞书开放平台创建应用</a>'
                  f'</div>')
    return (
        f'<div class="msg-edit" id="msg-edit-{grp}" style="display:none;">'
        f'<div class="msg-edit-title">{p["name"]} 配置</div>'
        f'{qr_btn}'
        f'{"".join(inputs)}'
        f'<div class="p-edit-btns">'
        f'<button type="button" class="p-edit-save" onclick="saveMsgCard(\'{grp}\')">保存</button>'
        f'<button type="button" class="p-edit-cancel" onclick="closeMsgCard(\'{grp}\')">取消</button>'
        f'</div>'
        f'</div>'
    )


def _render_default_model(cfg):
    """渲染「默认模型」配置卡片: 显示并允许修改默认模型."""
    model = cfg.get("LLM_MODEL", "")
    base = cfg.get("LLM_BASE_URL", "")
    # 按 base_url 推断当前默认供应商
    prov_name = "未指定"
    base_map = {
        "http://127.0.0.1:20128": "9Router（本机）",
        "api.deepseek.com": "DeepSeek",
        "api.xiaomimimo.com": "Xiaomi MiMo",
    }
    for hint, nm in base_map.items():
        if hint in (base or ""):
            prov_name = nm
            break
    opts = "".join(
        f'<option value="{p["key"]}"{" selected" if prov_name == p["name"] else ""}>{p["name"]}</option>'
        for p in MODEL_PROVIDERS
    )
    opts += '<option value="custom">自定义 URL</option>'
    cur = f'<div class="dm-current"><b>{model or "（未设置）"}</b> @ {base or "（未设置）"} · {prov_name}</div>'
    return (
        '<div class="cfg-section" id="default-model-sec">'
        '<div class="cfg-section-title">🎯 默认模型</div>'
        + cur
        + '<div style="font-size:11px;color:var(--muted);margin:6px 0 10px;">修改默认模型（供应商 / 模型名 / Base URL），保存后重启内核生效。</div>'
        '<div class="field-row">'
        '<div class="field-col"><label>供应商</label>'
        f'<select id="dm-provider" class="dm-input" onchange="dmProviderChanged()">{opts}</select></div>'
        '<div class="field-col"><label>模型名</label>'
        f'<input type="text" id="dm-model" class="dm-input" value="{model}" placeholder="如 deepseek-chat"></div>'
        '<div class="field-col"><label>Base URL</label>'
        f'<input type="text" id="dm-base" class="dm-input" value="{base}" placeholder="如 https://api.example.com/v1"></div>'
        '</div>'
        '<div class="btn-row" style="margin-top:10px;">'
        '<button class="save-btn" onclick="saveDefaultModel()">💾 保存默认模型</button>'
        '</div>'
        '</div>'
    )


def _render_providers_grid(cfg):
    """渲染模型供应商卡片网格 (参考 9Router providers)."""
    cards = []
    for p in MODEL_PROVIDERS:
        env_val = cfg.get(p["env"], "")
        connected = bool(env_val)
        status = "connected" if connected else "pending"
        status_txt = "● 已连接" if connected else "○ 未配置"
        badge = '<span class="p-badge">默认</span>' if p["default"] else ('<span class="p-badge">本地</span>' if p.get("local") else "")
        ico_bg = p["bg"]
        cards.append(
            f'<div class="provider-card" data-provider="{p["key"]}" onclick="editProvider(\'{p["key"]}\')">'
            f'{badge}'
            f'<div class="p-ico" style="background:{ico_bg};color:#fff;">{p["ico"]}</div>'
            f'<div class="p-name">{p["name"]}</div>'
            f'<span class="p-status {status}">{status_txt}</span>'
            f'<div class="p-desc">{p["desc"]}</div>'
            f'</div>'
        )
    return '<div class="providers-grid">' + "".join(cards) + "</div>"


# ── 微信 QR 扫码登录 (集成 Hermes gateway.platforms.weixin 原生 iLink 机制) ──
# 依赖: 应用 venv 里的 hermes-agent (gateway.platforms.weixin). 不可用则优雅降级.
def _weixin_module():
    """返回 weixin adapter 模块, 不可导入时返回 None."""
    try:
        import sys
        from gateway.platforms import weixin
        return weixin
    except Exception:
        pass
    # 兜底: 尝试把应用 venv 的 site-packages 加入 sys.path 再 import
    try:
        import glob
        for sp in glob.glob("/vol4/@appdata/HermesCore/venv/lib/python*/site-packages"):
            if sp not in sys.path:
                sys.path.insert(0, sp)
        from gateway.platforms import weixin
        return weixin
    except Exception:
        return None


def _weixin_qr_available():
    """是否可用 (依赖 weixin adapter + aiohttp/cryptography)."""
    m = _weixin_module()
    if m is None:
        return False
    try:
        return bool(m.check_weixin_requirements())
    except Exception:
        return False


def _weixin_qr_start(bot_type="3"):
    """获取微信登录二维码. 返回 dict {qrcode_url, qrcode_value} 或错误."""
    m = _weixin_module()
    if m is None:
        return {"error": "微信适配器不可用 (未安装 hermes-agent 或无法导入)"}
    try:
        import asyncio
        async def _fetch():
            import aiohttp
            async with aiohttp.ClientSession(trust_env=True) as session:
                resp = await m._api_get(
                    session,
                    base_url=m.ILINK_BASE_URL,
                    endpoint=f"{m.EP_GET_BOT_QR}?bot_type={bot_type}",
                    timeout_ms=m.QR_TIMEOUT_MS,
                )
                return resp
        resp = asyncio.run(_fetch())
        value = str(resp.get("qrcode") or "")
        url = str(resp.get("qrcode_img_content") or "")
        if not value:
            return {"error": "二维码响应缺少 qrcode"}
        return {"qrcode_url": url or value, "qrcode_value": value}
    except Exception as e:
        return {"error": f"获取二维码失败: {e}"}


def _weixin_qr_poll(qrcode_value):
    """轮询扫码状态. confirmed 时自动写 gateway.env. 返回 dict."""
    m = _weixin_module()
    if m is None:
        return {"error": "微信适配器不可用"}
    try:
        import asyncio
        async def _poll():
            import aiohttp
            async with aiohttp.ClientSession(trust_env=True) as session:
                resp = await m._api_get(
                    session,
                    base_url=m.ILINK_BASE_URL,
                    endpoint=f"{m.EP_GET_QR_STATUS}?qrcode={qrcode_value}",
                    timeout_ms=m.QR_TIMEOUT_MS,
                )
                return resp
        resp = asyncio.run(_poll())
        status = str(resp.get("status") or "wait")
        if status == "confirmed":
            account_id = str(resp.get("ilink_bot_id") or "")
            token = str(resp.get("bot_token") or "")
            base_url = str(resp.get("baseurl") or m.ILINK_BASE_URL)
            user_id = str(resp.get("ilink_user_id") or "")
            if not account_id or not token:
                return {"status": status, "error": "扫码确认但凭据不完整"}
            # 写 gateway.env (WEIXIN_ACCOUNT_ID/TOKEN/BASE_URL/CDN_BASE_URL)
            data = {
                "WEIXIN_ACCOUNT_ID": account_id,
                "WEIXIN_TOKEN": token,
                "WEIXIN_BASE_URL": base_url,
                "WEIXIN_CDN_BASE_URL": "https://novac2c.cdn.weixin.qq.com/c2c",
            }
            _save_config(data)
            return {"status": status, "account_id": account_id}
        return {"status": status}
    except Exception as e:
        return {"error": f"轮询扫码状态失败: {e}"}


class Handler(BaseHTTPRequestHandler):
    def _check_auth(self):
        """Bearer API key 鉴权."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] == API_KEY:
            return True
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": False, "error": "unauthorized"}).encode())
        return False

    def _handle_ws_pty(self):
        """处理 /api/pty 的 WebSocket 握手, 进入 PTY 终端循环."""
        upgrade = self.headers.get("Upgrade", "").lower()
        key = self.headers.get("Sec-WebSocket-Key", "")
        if upgrade != "websocket" or not key:
            self._json({"ok": False, "error": "websocket upgrade required"}, 400)
            return

        # 读取网关用户 Header (fnOS 统一网关转发)
        gw_user = self.headers.get("X-Trim-Userid", "")

        accept = _ws_accept_key(key)
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        # 长连接: 脱离 handler, 直接操作底层 socket
        sock = self.connection
        sock.settimeout(1)
        _handle_pty_ws(self, sock, {})

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _stream_chat(self, messages):
        """SSE 流式聊天响应."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        gen, err, captured_model = _chat_proxy(messages, stream=True)
        if err:
            self.wfile.write(f"event: error\ndata: {json.dumps({'error': err})}\n\n".encode())
            self.wfile.flush()
            return
        try:
            for piece in gen:
                if piece:
                    self.wfile.write(f"data: {json.dumps({'text': piece})}\n\n".encode())
                    self.wfile.flush()
        except Exception:
            pass
        try:
            # 发送 model 元信息 (前端用于展示 【耗时 · 模型名】)
            mdl = captured_model[0] if captured_model else ""
            self.wfile.write(f"event: meta\ndata: {json.dumps({'model': mdl})}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception:
            pass

    def _strip_gateway_prefix(self):
        """剥离 fnOS 统一网关前缀 (如 /app/hermescore), 按内部路径继续处理.

        网关把 /app/hermescore 或 /app/hermescore/api/... 转发到 app.sock,
        BaseHTTPRequestHandler 的 self.path 会是带前缀的完整路径, 需归一化。
        """
        p = self.path
        for prefix in ("/app/hermescore", "/app/HermesCore", "/app/Hermescore"):
            if p == prefix or p.startswith(prefix + "/") or p.startswith(prefix + "?"):
                rest = p[len(prefix):]
                self.path = rest if rest else "/"
                break

    def do_GET(self):  # noqa: N802
        self._strip_gateway_prefix()
        if self.path.startswith("/vendor/"):
            # 本地静态资源 (xterm.js 等)
            name = os.path.basename(self.path)
            fpath = os.path.join(VENDOR_DIR, name)
            if not os.path.isfile(fpath):
                self._json({"ok": False, "error": "not found"}, 404)
                return
            ext = os.path.splitext(name)[1].lower()
            ctype = VENDOR_TYPES.get(ext, "application/octet-stream")
            with open(fpath, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/version":
            # 应用版本 (品牌区/前端动态填充; 参考 Strava/hugo bootstrap)
            self._json({"version": APP_VERSION})
        if self.path == "/":
            self._render_page()
        elif self.path.startswith("/api/pty"):
            # PTY 终端 WebSocket (官方统一网关转发: /app/hermescore/api/pty)
            self._handle_ws_pty()
        elif self.path == "/api/config":
            if not self._check_auth():
                return
            self._json({"ok": True, "config": _load_config()})
        elif self.path.startswith("/api/weixin/qr/status"):
            if not self._check_auth():
                return
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query).get("qrcode", [""])[0]
            if not q:
                self._json({"ok": False, "error": "缺少 qrcode 参数"})
                return
            result = _weixin_qr_poll(q)
            self._json({"ok": "error" not in result, **result})
        elif self.path == "/api/logs/list":
            # 可用日志源列表
            if not self._check_auth():
                return
            srcs = []
            for k, v in LOG_SOURCES.items():
                if v and os.path.isfile(v):
                    try:
                        srcs.append({"key": k, "label": LOG_LABELS.get(k, k), "size": os.path.getsize(v)})
                    except OSError:
                        pass
            self._json({"ok": True, "sources": srcs})
        elif self.path.startswith("/api/logs"):
            # /api/logs?source=core&tail=300  或 /api/logs/download?source=core
            if not self._check_auth():
                return
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            name = q.get("source", ["core"])[0]
            if name not in LOG_SOURCES:
                self._json({"ok": False, "error": "unknown source"})
                return
            is_download = self.path.startswith("/api/logs/download")
            try:
                tail = int(q.get("tail", ["300"])[0])
            except ValueError:
                tail = 300
            content, ok = _read_log_source(name, 0 if is_download else tail)
            if not ok:
                self._json({"ok": False, "error": "log file not found"})
                return
            if is_download:
                body = content.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{name}.log"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"ok": True, "source": name, "content": content})
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        self._strip_gateway_prefix()
        if self.path == "/api/config":
            if not self._check_auth():
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length).decode()) if length else {}
            except Exception:
                data = {}
            # 只接受白名单字段
            allowed = {k for k, _, _, _ in CONFIG_FIELDS}
            clean = {k: (v or "").strip() for k, v in data.items() if k in allowed}
            ok, err = _save_config(clean)
            self._json({"ok": ok, "error": err} if not ok else {"ok": True})
        elif self.path == "/api/weixin/qr/start":
            if not self._check_auth():
                return
            if not _weixin_qr_available():
                self._json({"ok": False, "error": "微信 QR 登录不可用 (需 hermes-agent 的 weixin 适配器)"})
                return
            result = _weixin_qr_start()
            if "error" in result:
                self._json({"ok": False, "error": result["error"]})
            else:
                self._json({"ok": True, **result})
        elif self.path == "/api/restart":
            if not self._check_auth():
                return
            ok, err = _do_restart()
            self._json({"ok": ok, "error": err} if not ok else {"ok": True})
        elif self.path == "/api/chat":
            # 聊天: 代理到本机 api_server (8642) 的 /v1/chat/completions
            if not self._check_auth():
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length).decode()) if length else {}
            except Exception:
                data = {}
            messages = data.get("messages", [])
            want_stream = bool(data.get("stream"))
            if want_stream:
                self._stream_chat(messages)
            else:
                reply, err = _chat_proxy(messages)
                if reply is not None:
                    self._json({"ok": True, "reply": reply})
                else:
                    self._json({"ok": False, "error": err or "chat failed"})
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def _render_page(self):
        ok, info = _core_health()
        if ok:
            status_cls, status_text = "ok", "● 运行中"
            state = "healthy"
            platform = info.get("platform", "-")
            version = info.get("version", "-")
        else:
            status_cls, status_text = "down", "● 未运行"
            state = info.get("error", "unreachable")
            platform = version = "-"

        # 消息网关状态
        gw = _gateway_status()
        if gw.get("state") == "running":
            gw_cls, gw_text = "ok", "● 运行中"
        elif gw.get("state") == "unknown" and not gw.get("platforms"):
            gw_cls, gw_text = "down", "● 未知"
        else:
            gw_cls, gw_text = "down", "● " + str(gw.get("state", "未知"))
        gw_platforms = []
        plats = gw.get("platforms", {})
        if plats:
            for name, p in plats.items():
                pstate = p.get("state", "?") if isinstance(p, dict) else "?"
                if isinstance(p, dict) and p.get("state") == "connected":
                    ptext = f'{name} <span class="ok" style="font-size:11px">● 在线</span>'
                else:
                    err = p.get("error_message") or (p.get("error_code") or "") if isinstance(p, dict) else ""
                    ptext = f'{name} <span class="down" style="font-size:11px">● {pstate}</span>'
                gw_platforms.append(f'<div class="row"><span class="label">{ptext}</span></div>')
        else:
            gw_platforms.append('<div class="row"><span class="label" style="color:#999">未检测到平台</span></div>')
        # 紧凑版 (mini 卡片)
        gw_platforms_min = []
        if plats:
            for name, p in plats.items():
                pstate = p.get("state", "?") if isinstance(p, dict) else "?"
                ptext = f'{name}: <b>{pstate}</b>'
                gw_platforms_min.append(f'<div>{ptext}</div>')
        else:
            gw_platforms_min.append('<div style="color:#999">未检测到平台</div>')

        # 兜底 LLM 状态
        llm = _llm_status()
        if llm.get("ok"):
            llm_cls, llm_text = "ok", "● 连接正常"
        elif llm.get("configured"):
            llm_cls, llm_text = "down", "● 连接失败"
        else:
            llm_cls, llm_text = "down", "○ 未配置"
        llm_rows = []
        llm_rows.append(f'<div class="row"><span class="label">状态</span><span class="val">{llm.get("msg", "")}</span></div>')
        if llm.get("model"):
            llm_rows.append(f'<div class="row"><span class="label">模型</span><span class="val">{llm["model"]}</span></div>')
        if llm.get("models"):
            llm_rows.append(f'<div class="row"><span class="label">可用模型</span><span class="val">{"，".join(llm["models"])}</span></div>')
        # 紧凑版 (mini 卡片)
        llm_rows_min = []
        llm_rows_min.append(f'<div>状态: <b>{llm.get("msg", "")}</b></div>')
        if llm.get("model"):
            llm_rows_min.append(f'<div>模型: <b>{llm["model"]}</b></div>')

        # Dashboard 状态
        dash = _dashboard_status()
        if not dash.get("enabled"):
            dash_cls, dash_text, dash_detail = "down", "○ 未启用", "已在配置中关闭"
        elif dash.get("ok"):
            dash_cls, dash_text, dash_detail = "ok", "● 运行中", "运行中"
        else:
            dash_cls, dash_text, dash_detail = "down", "● 已启用未运行", "未运行（重启内核生效）"
        dash_user = dash.get("user", "-")
        dash_port = dash.get("port", 9119)

        cfg = _load_config()
        html = PAGE.format(
            STATUS_CLS=status_cls,
            STATUS_TEXT=status_text,
            STATE=state,
            PLATFORM=platform,
            VERSION=version,
            CORE_PORT=CORE_PORT,
            GW_CLS=gw_cls,
            GW_TEXT=gw_text,
            GW_PLATFORMS="\n".join(gw_platforms),
            GW_PLATFORMS_MIN="\n".join(gw_platforms_min),
            LLM_CLS=llm_cls,
            LLM_TEXT=llm_text,
            LLM_ROWS="\n".join(llm_rows),
            LLM_ROWS_MIN="\n".join(llm_rows_min),
            DASH_CLS=dash_cls,
            DASH_TEXT=dash_text,
            DASH_DETAIL=dash_detail,
            DASH_USER=dash_user,
            DASH_PORT=dash_port,
            FORM_FIELDS=_form_fields(cfg),
            MSG_FIELDS=_msg_fields(cfg),
            PROVIDERS_GRID=_render_providers_grid(cfg),
            DEFAULT_MODEL_HTML=_render_default_model(cfg),
            AUTH_TOKEN=json.dumps(API_KEY),   # 注入鉴权 token 到前端 JS
            LLM_MODEL_JSON=json.dumps(cfg.get("LLM_MODEL", "")),   # 注入默认模型名
            STATUS_VER=STATUS_VER,
            APP_VERSION=APP_VERSION,
            TS=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Server(ThreadingHTTPServer):
    allow_reuse_address = True


class UnixServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX
    allow_reuse_address = True


def main():
    # 启动 TCP 服务 (备用访问 :STATUS_PORT)
    tcp_server = Server((BIND_HOST, LISTEN_PORT), Handler)
    tcp_thread = threading.Thread(target=tcp_server.serve_forever, daemon=True)
    tcp_thread.start()
    print(f"status server on {BIND_HOST}:{LISTEN_PORT}")

    # 官方统一网关: 监听 Unix socket (如设置了 STATUS_SOCK)
    if SOCK_PATH:
        sock_dir = os.path.dirname(SOCK_PATH)
        if sock_dir and not os.path.exists(sock_dir):
            try:
                os.makedirs(sock_dir, exist_ok=True)
            except OSError:
                pass
        if os.path.exists(SOCK_PATH):
            try:
                os.remove(SOCK_PATH)
            except OSError:
                pass
        try:
            unix_server = UnixServer(SOCK_PATH, Handler)
            unix_thread = threading.Thread(target=unix_server.serve_forever, daemon=True)
            unix_thread.start()
            print(f"status server on unix socket {SOCK_PATH}")
        except Exception as e:
            print(f"unix socket bind failed: {e}")

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
