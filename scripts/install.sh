#!/usr/bin/env bash
set -euo pipefail

# OpenCode Mesh 一键安装（在目标设备本机执行）
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | bash
#   或指定模式：
#   curl -fsSL .../install.sh | bash -s -- agent
#   curl -fsSL .../install.sh | bash -s -- gateway
#
# 支持环境变量预填（非交互）：
#   MESH_MODE=agent MESH_GATEWAY_URL=... MESH_ENROLL_TOKEN=... \
#   OPENCODE_URL=... OPENCODE_USERNAME=... OPENCODE_PASSWORD=... \
#   bash install.sh

info() { printf '\033[1;34m[mesh]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[mesh]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[mesh]\033[0m %s\n' "$*" >&2; }

ask() {
  local prompt="$1" default="$2" value=""
  if [ -t 0 ]; then
    read -rp "${prompt} [${default}]: " value || true
  elif [ -e /dev/tty ]; then
    read -rp "${prompt} [${default}]: " value < /dev/tty || true
  fi
  printf '%s' "${value:-$default}"
}

MODE="${1:-${MESH_MODE:-}}"

# 检测运行身份，决定 systemd 级别和安装目录
if [ "$(id -u)" -eq 0 ]; then
  INSTALL_DIR="${MESH_INSTALL_DIR:-/opt/opencode-mesh}"
  SYSTEMD_KIND="system"
  UNIT_DIR="/etc/systemd/system"
else
  INSTALL_DIR="${MESH_INSTALL_DIR:-$HOME/.local/share/opencode-mesh}"
  SYSTEMD_KIND="user"
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
fi

command -v python3 >/dev/null 2>&1 || { err "缺少 python3，请先安装"; exit 1; }
command -v curl   >/dev/null 2>&1 || { err "缺少 curl，请先安装"; exit 1; }
command -v tar    >/dev/null 2>&1 || { err "缺少 tar，请先安装"; exit 1; }

if [ -z "$MODE" ]; then
  printf '请选择安装模式：\n'
  printf '  1) agent   运行在 OpenCode 所在设备，代理本机 OpenCode\n'
  printf '  2) gateway 运行在公网服务器，负责认证与设备路由\n'
  MODE="$(ask "输入 1 或 2" "")"
  case "$MODE" in
    1|agent) MODE="agent" ;;
    2|gateway) MODE="gateway" ;;
    *) err "无效选择"; exit 1 ;;
  esac
fi

if [ "$MODE" != "agent" ] && [ "$MODE" != "gateway" ]; then
  err "模式必须是 agent 或 gateway"; exit 1
fi

SERVICE_NAME="opencode-mesh-${MODE}"
VERSION="main"
TARBALL="https://github.com/RayDutchman/opencode-mesh/archive/refs/heads/${VERSION}.tar.gz"

info "安装目录：${INSTALL_DIR}"
info "systemd 级别：${SYSTEMD_KIND}"
info "下载 opencode-mesh 源码（${VERSION}）..."

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fsSL "$TARBALL" -o "$tmp/mesh.tar.gz"
tar -xzf "$tmp/mesh.tar.gz" -C "$tmp"
SRC="$(find "$tmp" -maxdepth 1 -type d -name 'opencode-mesh-*' | head -n 1)"

mkdir -p "$INSTALL_DIR"
if [ -d "$INSTALL_DIR/src" ]; then
  warn "检测到已有安装，覆盖源码与脚本（保留 config 与 data）"
  rm -rf "$INSTALL_DIR/src" "$INSTALL_DIR/scripts" "$INSTALL_DIR/pyproject.toml"
fi
cp -r "$SRC"/src "$SRC"/pyproject.toml "$SRC"/scripts "$INSTALL_DIR"/ 2>/dev/null || true

if [ ! -d "$INSTALL_DIR/.venv" ]; then
  info "创建 Python 虚拟环境..."
  python3 -m venv "$INSTALL_DIR/.venv" 2>/dev/null || python3 -m venv --system-site-packages "$INSTALL_DIR/.venv"
fi

info "安装依赖（首次可能较慢，aiortc 需要编译或下载 wheel）..."
"$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip -q
"$INSTALL_DIR/.venv/bin/python" -m pip install -e "$INSTALL_DIR" -q

mkdir -p "$INSTALL_DIR/config" "$INSTALL_DIR/data"

info "配置 ${MODE} ..."
if [ "$MODE" = "agent" ]; then
  GATEWAY_URL="$(ask "Gateway 公网地址（必填）" "${MESH_GATEWAY_URL:-}")"
  ENROLL_TOKEN="$(ask "enroll_token（必填）" "${MESH_ENROLL_TOKEN:-}")"
  OPENCODE_URL="$(ask "本机 OpenCode 地址（含端口）" "${OPENCODE_URL:-http://127.0.0.1:40960}")"
  OPENCODE_USERNAME="$(ask "OpenCode 用户名（未启用认证则留空）" "${OPENCODE_USERNAME:-}")"
  OPENCODE_PASSWORD="$(ask "OpenCode 密码（未启用认证则留空）" "${OPENCODE_PASSWORD:-}")"
  CONFIG_FILE="$INSTALL_DIR/config/agent.json"
  INSTALL_DIR="$INSTALL_DIR" GATEWAY_URL="$GATEWAY_URL" ENROLL_TOKEN="$ENROLL_TOKEN" \
    OPENCODE_URL="$OPENCODE_URL" OPENCODE_USERNAME="$OPENCODE_USERNAME" OPENCODE_PASSWORD="$OPENCODE_PASSWORD" \
    python3 - "$CONFIG_FILE" <<'PY'
import json, os, sys
cfg = {
    "listen_host": "127.0.0.1",
    "listen_port": 18090,
    "gateway_url": os.environ["GATEWAY_URL"].rstrip("/"),
    "enroll_token": os.environ["ENROLL_TOKEN"],
    "opencode_url": os.environ["OPENCODE_URL"].rstrip("/"),
    "state_file": os.path.join(os.environ["INSTALL_DIR"], "data", "agent-state.json"),
    "reconnect_seconds": 5,
}
if os.environ.get("OPENCODE_PASSWORD"):
    cfg["opencode_basic_auth"] = {
        "username": os.environ.get("OPENCODE_USERNAME") or "opencode",
        "password": os.environ["OPENCODE_PASSWORD"],
    }
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
  EXEC="$INSTALL_DIR/.venv/bin/python -m src.main --mode agent --config $CONFIG_FILE"
else
  LISTEN_PORT="$(ask "监听端口" "${MESH_LISTEN_PORT:-18080}")"
  USERNAME="$(ask "登录用户名" "${MESH_USERNAME:-opencode}")"
  PASSWORD="$(ask "登录密码（必填）" "${MESH_PASSWORD:-}")"
  ENROLL_TOKEN="$(ask "enroll_token（留空自动生成）" "${MESH_ENROLL_TOKEN:-}")"
  if [ -z "$ENROLL_TOKEN" ]; then
    ENROLL_TOKEN="$("$INSTALL_DIR/.venv/bin/python" -c 'import secrets;print(secrets.token_urlsafe(32))')"
    info "已生成 enroll_token：${ENROLL_TOKEN}"
  fi
  CONFIG_FILE="$INSTALL_DIR/config/gateway.json"
  INSTALL_DIR="$INSTALL_DIR" LISTEN_PORT="$LISTEN_PORT" USERNAME="$USERNAME" PASSWORD="$PASSWORD" ENROLL_TOKEN="$ENROLL_TOKEN" \
    python3 - "$CONFIG_FILE" <<'PY'
import json, os, sys
cfg = {
    "listen_host": "127.0.0.1",
    "listen_port": int(os.environ["LISTEN_PORT"]),
    "secure_cookie": False,
    "state_file": os.path.join(os.environ["INSTALL_DIR"], "data", "gateway-state.json"),
    "enroll_token": os.environ["ENROLL_TOKEN"],
    "default_device": "",
    "auth": {"username": os.environ["USERNAME"], "password": os.environ["PASSWORD"]},
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
  EXEC="$INSTALL_DIR/.venv/bin/python -m src.main --mode gateway --config $CONFIG_FILE"
fi

info "写入 systemd unit ..."
mkdir -p "$UNIT_DIR"
if [ "$SYSTEMD_KIND" = "user" ]; then
  UNIT_FILE="$UNIT_DIR/${SERVICE_NAME}.service"
  cat > "$UNIT_FILE" <<UNIT
[Unit]
Description=OpenCode Mesh ${MODE}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${EXEC}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now "${SERVICE_NAME}.service"
  info "启用用户级 linger（保证未登录时也运行）..."
  loginctl enable-linger "$(id -un)" 2>/dev/null || warn "无法启用 linger（无 loginctl），需保持登录会话"
else
  UNIT_FILE="$UNIT_DIR/${SERVICE_NAME}.service"
  cat > "$UNIT_FILE" <<UNIT
[Unit]
Description=OpenCode Mesh ${MODE}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${EXEC}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now "${SERVICE_NAME}.service"
fi

info "启动服务..."
if [ "$SYSTEMD_KIND" = "user" ]; then
  systemctl --user restart "${SERVICE_NAME}.service"
  systemctl --user --no-pager --full status "${SERVICE_NAME}.service" || true
else
  systemctl restart "${SERVICE_NAME}.service"
  systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
fi

info "安装完成。"
info "卸载：curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/uninstall.sh | bash -s -- ${MODE}"
