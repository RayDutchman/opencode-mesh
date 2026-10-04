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

# Serialize all lifecycle operations for this canonical installation directory.
command -v flock >/dev/null 2>&1 || { err "flock is required for Mesh lifecycle locking"; exit 1; }
command -v python3 >/dev/null 2>&1 || { err "python3 is required for Mesh lifecycle locking"; exit 1; }
INSTALL_KEY="$(printf '%s' "$(realpath -m "$INSTALL_DIR")" | sha256sum | awk '{print $1}')"
TMP_MODE="$(stat -c '%a' /tmp)"
[[ ! -L /tmp && $(stat -c '%u' /tmp) == 0 && "$TMP_MODE" =~ ^[0-7]+$ ]] && (( (8#$TMP_MODE & 8#1000) != 0 )) || { err "unsafe /tmp permissions for Mesh lifecycle locking"; exit 1; }
LOCK_DIR="/tmp/opencode-mesh-$INSTALL_KEY.lock"
LOCK_OWNER="$(id -u)"
if [[ -d "$INSTALL_DIR" ]]; then LOCK_OWNER="$(stat -Lc '%u' "$INSTALL_DIR")"; fi
if mkdir -m 700 "$LOCK_DIR" 2>/dev/null; then chown "$LOCK_OWNER" "$LOCK_DIR"; elif [[ ! -d "$LOCK_DIR" || -L "$LOCK_DIR" ]]; then err "unsafe Mesh lifecycle lock path: ${LOCK_DIR}"; exit 1; fi
[[ $(stat -c '%u:%a' "$LOCK_DIR") == "$LOCK_OWNER:700" ]] || { err "Mesh lifecycle lock is not owned by the installation owner: ${LOCK_DIR}"; exit 1; }
HOLDER="$LOCK_DIR/holder"
[[ ! -L "$HOLDER" && ( ! -e "$HOLDER" || ( -f "$HOLDER" && $(stat -c '%u:%a' "$HOLDER") == "$LOCK_OWNER:600" ) ) ]] || { err "unsafe Mesh lifecycle lock holder: ${HOLDER}"; exit 1; }
if [[ ! -e "$HOLDER" ]]; then
  (umask 077; set -C; : > "$HOLDER")
  chown "$LOCK_OWNER" "$HOLDER"
fi
LOCK_READY="$LOCK_DIR/.ready.$$.${RANDOM}"
PARENT_START="$(awk '{print $22}' "/proc/$$/stat")"
flock -n "$HOLDER" python3 - "$$" "$PARENT_START" "$LOCK_READY" <<'PY' >/dev/null 2>&1 &
import os, sys, time
from pathlib import Path
pid, start, ready = sys.argv[1:]
Path(ready).touch()
while True:
    try:
        if Path(f"/proc/{pid}/stat").read_text().split()[21] != start: break
    except FileNotFoundError: break
    time.sleep(.001)
PY
LOCK_GUARDIAN=$!
for _ in $(seq 1 100); do [[ -e "$LOCK_READY" ]] && break; kill -0 "$LOCK_GUARDIAN" 2>/dev/null || break; sleep .01; done
[[ -e "$LOCK_READY" ]] || { wait "$LOCK_GUARDIAN" 2>/dev/null || true; err "another Mesh lifecycle operation is using ${INSTALL_DIR}"; exit 1; }
rm -f "$LOCK_READY"

# A shared supervisor unit holds every configured instance, including the default one.
# Only ExecStart counts: a mention in a comment must not turn a legacy unit into a shared one.
shared_agent_unit_installed() {
  [ "$MODE" = agent ] && [ -f "$UNIT_DIR/opencode-mesh-agent.service" ] &&
    python3 - "$UNIT_DIR/opencode-mesh-agent.service" <<'PY'
import shlex, sys
for line in open(sys.argv[1]):
    if not line.startswith("ExecStart="):
        continue
    if "--all-instances" in shlex.split(line.split("=", 1)[1].strip())[1:]:
        sys.exit(0)
sys.exit(1)
PY
}

if [ "$MODE" = all ]; then
  SERVICES=("opencode-mesh-agent" "opencode-mesh-gateway")
  for unit in "$UNIT_DIR"/opencode-mesh-agent@*.service; do
    [[ -f "$unit" && "$unit" != *'@.service' ]] || continue
    SERVICES+=("$(basename "$unit" .service)")
  done
elif shared_agent_unit_installed && [ "$INSTANCE" != default ]; then
  # There is no per-instance unit to remove; the shared one owns this instance too.
  SERVICES=("opencode-mesh-agent")
else
  SERVICES=("opencode-mesh-${MODE}")
  [[ "$MODE" != agent || "$INSTANCE" == default ]] || SERVICES=("opencode-mesh-agent@$INSTANCE")
fi

# A shared supervisor unit outlives any single instance; only the last one may take it down.
REMAINING_INSTANCES="$(python3 - "$INSTALL_DIR" "$MODE" "$INSTANCE" <<'PY'
import json, sys
from pathlib import Path
root, mode, selected = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
path = root / "config" / "agents.json"
names = list(json.loads(path.read_text()).get("agents", {})) if path.exists() else []
if mode == "all":
    names = []
else:
    names = [name for name in names if name != selected]
print(len(names))
PY
)"

SHARED_UNIT=""
SHARED_WAS_ACTIVE=0
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
    if [ "$MODE" != all ] && shared_agent_unit_installed && [ "$svc" = "opencode-mesh-agent" ] &&
       [ "$REMAINING_INSTANCES" != 0 ]; then
      # The Gateway refuses to drop a device whose Agent is still connected, so the shared
      # unit has to stop first. Its previous state is restored after the configuration
      # change, and a unit that was already stopped stays stopped.
      SHARED_UNIT="$svc"
      if $SC_CMD is-active --quiet "${svc}.service"; then
        SHARED_WAS_ACTIVE=1
        info "stopping ${svc} to close the Agent connection before deregistration ..."
        $SC_CMD stop "${svc}.service" || { err "could not stop ${svc}; the instance stays registered"; exit 1; }
      else
        info "${svc} is not running; no Agent connection to close"
      fi
      continue
    fi
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

# The configuration no longer names the removed instance, so the shared unit can serve the rest.
if [ -n "$SHARED_UNIT" ] && [ "$SHARED_WAS_ACTIVE" = 1 ]; then
  info "restarting ${SHARED_UNIT} for the remaining instances ..."
  $SC_CMD restart "${SHARED_UNIT}.service"
fi

# Single-instance uninstall keeps the shared source and identity so that other stopped instances are unaffected.
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
