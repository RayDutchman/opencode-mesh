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
    printf '%s [%s]: ' "$prompt" "$default" >&2
    read -r value < "$TTY_AVAILABLE" 2>/dev/null || value=""
  fi
  printf '%s' "${value:-$default}"
}

MODE="${1:-${MESH_MODE:-}}"
[[ $# -le 2 ]] || exit 2
INSTANCE="${2-${MESH_INSTANCE:-default}}"
if [[ -z "$MODE" && -n "$TTY_AVAILABLE" ]]; then
  MODE=$(ask 'Uninstall mode (agent/gateway/all)' agent)
  [[ "$MODE" != agent ]] || INSTANCE=$(ask 'Agent instance' default)
  answer=$(ask 'Continue (y/N)' N)
  [[ "$answer" == y || "$answer" == Y ]] || exit 0
fi
[[ "$INSTANCE" =~ ^[A-Za-z0-9_-]+$ ]] || exit 2
[[ $# != 2 || ( "$MODE" == agent && "$INSTANCE" != default ) ]] || exit 2
case "$MODE" in
  agent|gateway|all) ;;
  "") err "mode is required; use agent, gateway, or all"; exit 2 ;;
  *) err "invalid mode '$MODE'; use agent, gateway, or all"; exit 2 ;;
esac

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
  for unit in "$UNIT_DIR"/opencode-mesh-agent@*.service; do
    [[ -f "$unit" && "$unit" != *'@.service' ]] || continue
    SERVICES+=("$(basename "$unit" .service)")
  done
else
  SERVICES=("opencode-mesh-${MODE}")
  [[ "$MODE" != agent || "$INSTANCE" == default ]] || SERVICES=("opencode-mesh-agent@$INSTANCE")
fi

for svc in "${SERVICES[@]}"; do
  if [ -f "${UNIT_DIR}/${svc}.service" ]; then
    wd=$(python3 - "${UNIT_DIR}/${svc}.service" <<'PY'
import sys
for line in open(sys.argv[1]):
    if line.startswith("WorkingDirectory="):
        print(line.split("=", 1)[1].strip().strip('"'))
PY
)
    [[ -n "$wd" && "$(realpath "$wd")" == "$(realpath "$INSTALL_DIR")" ]] || { err "service belongs to another directory"; exit 1; }
    info "stopping and disabling ${svc} ..."
    $SC_CMD stop "${svc}.service"
    $SC_CMD disable "${svc}.service" 2>/dev/null || true
    rm -f "${UNIT_DIR}/${svc}.service"
  fi
done
$SC_CMD daemon-reload 2>/dev/null || true

if [[ "$MODE" != gateway && -f "$INSTALL_DIR/config/agents.json" ]]; then
  while IFS= read -r gateway && IFS= read -r device && IFS= read -r token; do
    curl -fsSL --max-time 15 -H "X-Mesh-Agent-Token: $token" -X DELETE \
      "${gateway%/}/_mesh/deregister/$device" >/dev/null 2>&1 || warn "device deregistration failed"
  done < <(python3 - "$INSTALL_DIR" "$MODE" "$INSTANCE" <<'PY'
import json, sys
from pathlib import Path
root, mode, selected = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
cfg = json.loads((root / "config" / "agents.json").read_text())
for name, entry in cfg.get("agents", {}).items():
    if mode != "all" and name != selected:
        continue
    state = root / "data" / ("agent-state.json" if name == "default" else f"agent-state-{name}.json")
    if not state.exists():
        continue
    identity = json.loads(state.read_text())
    values = [entry.get("gateway_url", cfg.get("gateway_url", "")), identity.get("device_id", ""), identity.get("agent_token", "")]
    if all(isinstance(v, str) and v and "\n" not in v and "\r" not in v for v in values):
        print("\n".join(values))
PY
)
  python3 - "$INSTALL_DIR/config/agents.json" "$MODE" "$INSTANCE" <<'PY'
import json, os, sys, tempfile
path, mode, name = sys.argv[1:]
cfg = json.load(open(path))
if mode == "all":
    cfg["agents"] = {}
else:
    cfg.get("agents", {}).pop(name, None)
fd, temporary = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w") as handle:
    json.dump(cfg, handle, indent=2)
os.replace(temporary, path)
PY
fi

# 单实例卸载始终保留共享源码和内部身份，避免影响其他停止中的实例。
if [[ "$MODE" != all ]]; then
  info "instance removed; shared installation and identity retained"
  exit 0
fi

# Tell the Gateway to drop the agent registration so no offline device remains.
if [ "$MODE" != "gateway" ] && [ -f "$INSTALL_DIR/config/agent.json" ] && [ -f "$INSTALL_DIR/data/agent-state.json" ]; then
  GATEWAY_URL="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("gateway_url",""))' "$INSTALL_DIR/config/agent.json" 2>/dev/null || true)"
  DEVICE_ID="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("device_id",""))' "$INSTALL_DIR/data/agent-state.json" 2>/dev/null || true)"
  TOKEN="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("agent_token",""))' "$INSTALL_DIR/data/agent-state.json" 2>/dev/null || true)"
  if [ -n "$GATEWAY_URL" ] && [ -n "$DEVICE_ID" ] && [ -n "$TOKEN" ]; then
    info "deregistering device ${DEVICE_ID} ..."
    curl -fsSL --max-time 15 -H "X-Mesh-Agent-Token: ${TOKEN}" -X DELETE "${GATEWAY_URL}/_mesh/deregister/${DEVICE_ID}" >/dev/null 2>&1 || warn "device deregistration failed; a stale gateway registration may remain"
  fi
fi

if [ -d "$INSTALL_DIR" ]; then
  KEEP="${MESH_KEEP_DATA:-}"
  if [ -z "$KEEP" ]; then
    KEEP="$(ask "Keep the data/ directory (device identity and state)? (y/N)" "N")"
  fi
  case "$KEEP" in
    y|Y|n|N|"") ;;
    *) err "MESH_KEEP_DATA must be y or n"; exit 2 ;;
  esac
  if [ "$KEEP" = "y" ] || [ "$KEEP" = "Y" ]; then
    info "removing install files while preserving ${INSTALL_DIR}/data ..."
    shopt -s dotglob nullglob
    for item in "$INSTALL_DIR"/*; do
      [ "$(basename "$item")" = "data" ] || rm -rf "$item"
    done
    shopt -u dotglob nullglob
  else
    info "removing install directory ${INSTALL_DIR} ..."
    rm -rf "$INSTALL_DIR"
  fi
fi

info "uninstall complete."
