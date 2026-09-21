#!/usr/bin/env bash
set -euo pipefail

# OpenCode Mesh 一键卸载（在目标设备本机执行）
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/uninstall.sh | bash -s -- agent
#   curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/uninstall.sh | bash -s -- gateway
#   或全部卸载：
#   .../uninstall.sh | bash -s -- all

info() { printf '\033[1;34m[mesh]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[mesh]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[mesh]\033[0m %s\n' "$*" >&2; }

ask() {
  local prompt="$1" default="$2" value=""
  if [ -t 1 ]; then
    read -rp "${prompt} [${default}]: " value < /dev/tty 2>/dev/null || true
  fi
  printf '%s' "${value:-$default}"
}

MODE="${1:-${MESH_MODE:-all}}"

if [ "$(id -u)" -eq 0 ]; then
  INSTALL_DIR="${MESH_INSTALL_DIR:-/opt/opencode-mesh}"
  SYSTEMD_KIND="system"
  UNIT_DIR="/etc/systemd/system"
  SC_CMD="systemctl"
else
  INSTALL_DIR="${MESH_INSTALL_DIR:-$HOME/.local/share/opencode-mesh}"
  SYSTEMD_KIND="user"
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  SC_CMD="systemctl --user"
fi

if [ "$MODE" = "all" ]; then
  SERVICES=("opencode-mesh-agent" "opencode-mesh-gateway")
else
  SERVICES=("opencode-mesh-${MODE}")
fi

for svc in "${SERVICES[@]}"; do
  if $SC_CMD list-unit-files 2>/dev/null | grep -q "^${svc}\."; then
    info "停止并禁用 ${svc} ..."
    $SC_CMD stop "${svc}.service" 2>/dev/null || true
    $SC_CMD disable "${svc}.service" 2>/dev/null || true
    rm -f "${UNIT_DIR}/${svc}.service"
  fi
done
$SC_CMD daemon-reload 2>/dev/null || true

if [ -d "$INSTALL_DIR" ]; then
  KEEP="$(ask "是否保留 data/ 目录（含设备身份/状态）？(y/N)" "N")"
  if [ "$KEEP" = "y" ] || [ "$KEEP" = "Y" ]; then
    BACKUP="$(mktemp -d)/opencode-mesh-data"
    mkdir -p "$BACKUP"
    [ -d "$INSTALL_DIR/data" ] && cp -r "$INSTALL_DIR/data" "$BACKUP/" 2>/dev/null || true
    info "data 已备份到 ${BACKUP}"
  fi
  info "删除安装目录 ${INSTALL_DIR} ..."
  rm -rf "$INSTALL_DIR"
fi

info "卸载完成。"
