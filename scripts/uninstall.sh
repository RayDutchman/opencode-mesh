#!/usr/bin/env bash
set -euo pipefail

# OpenCode Mesh uninstaller (run on the target device)
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/uninstall.sh | bash -s -- agent
#   curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/uninstall.sh | bash -s -- gateway
#   remove everything:
#   .../uninstall.sh | bash -s -- all

info() { printf '\033[1;34m[mesh]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[mesh]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[mesh]\033[0m %s\n' "$*" >&2; }

TTY_AVAILABLE=""
if { : >/dev/tty; } 2>/dev/null; then
  TTY_AVAILABLE="/dev/tty"
fi

ask() {
  local prompt="$1" default="$2" value=""
  if [ -n "$TTY_AVAILABLE" ]; then
    read -rp "${prompt} [${default}]: " value < "$TTY_AVAILABLE" 2>/dev/null || value=""
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
  if [ -f "${UNIT_DIR}/${svc}.service" ]; then
    info "stopping and disabling ${svc} ..."
    $SC_CMD stop "${svc}.service"
    $SC_CMD disable "${svc}.service" 2>/dev/null || true
    rm -f "${UNIT_DIR}/${svc}.service"
  fi
done
$SC_CMD daemon-reload 2>/dev/null || true

# Agent mode: tell the Gateway to drop the registration so no offline device remains.
if [ "$MODE" = "agent" ] && [ -f "$INSTALL_DIR/config/agent.json" ] && [ -f "$INSTALL_DIR/data/agent-state.json" ]; then
  GATEWAY_URL="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("gateway_url",""))' "$INSTALL_DIR/config/agent.json" 2>/dev/null || true)"
  DEVICE_ID="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("device_id",""))' "$INSTALL_DIR/data/agent-state.json" 2>/dev/null || true)"
  TOKEN="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("agent_token",""))' "$INSTALL_DIR/data/agent-state.json" 2>/dev/null || true)"
  if [ -n "$GATEWAY_URL" ] && [ -n "$DEVICE_ID" ] && [ -n "$TOKEN" ]; then
    info "deregistering device ${DEVICE_ID} ..."
    curl -fsSL --max-time 15 -H "X-Mesh-Agent-Token: ${TOKEN}" -X DELETE "${GATEWAY_URL}/_mesh/deregister/${DEVICE_ID}" >/dev/null 2>&1 || warn "device deregistration failed; a stale gateway registration may remain"
  fi
fi

if [ -d "$INSTALL_DIR" ]; then
  KEEP="$(ask "Keep the data/ directory (device identity and state)? (y/N)" "N")"
  if [ "$KEEP" = "y" ] || [ "$KEEP" = "Y" ]; then
    BACKUP="$(mktemp -d)/opencode-mesh-data"
    mkdir -p "$BACKUP"
    [ -d "$INSTALL_DIR/data" ] && cp -r "$INSTALL_DIR/data" "$BACKUP/" 2>/dev/null || true
    info "data backed up to ${BACKUP}"
  fi
  info "removing install directory ${INSTALL_DIR} ..."
  rm -rf "$INSTALL_DIR"
fi

info "uninstall complete."
