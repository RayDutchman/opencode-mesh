#!/usr/bin/env bash
set -euo pipefail

# Deploy the Mesh Agent to a Linux host that already runs OpenCode.
# Credentials are passed only through environment variables and never written to the repo;
# the remote config is written to config/agent.local.json.

usage() {
  cat <<'EOF'
Usage:
  MESH_GATEWAY_URL=https://oc.example.com \
  MESH_ENROLL_TOKEN=... \
  OPENCODE_URL=http://127.0.0.1:4096 \
  OPENCODE_USERNAME=opencode \
  OPENCODE_PASSWORD=... \
  scripts/deploy-agent.sh user@device [install_dir]

Optional environment variables:
  MESH_DEVICE_NAME      device display name, defaults to the remote hostname
  MESH_INSTALL_DIR      defaults to /opt/opencode-mesh
  MESH_PYTHON           defaults to python3
  MESH_GATEWAY_URL      Gateway public URL, required
  MESH_ENROLL_TOKEN     Gateway enrollment token, required
  OPENCODE_URL          local OpenCode URL, required
  OPENCODE_USERNAME     OpenCode Basic Auth username, defaults to opencode
  OPENCODE_PASSWORD     OpenCode Basic Auth password, may be empty
EOF
}

[[ $# -ge 1 && $# -le 2 ]] || { usage >&2; exit 2; }
REMOTE=$1
INSTALL_DIR=${2:-${MESH_INSTALL_DIR:-/opt/opencode-mesh}}
: "${MESH_GATEWAY_URL:?MESH_GATEWAY_URL is required}"
: "${MESH_ENROLL_TOKEN:?MESH_ENROLL_TOKEN is required}"
: "${OPENCODE_URL:?OPENCODE_URL is required}"
OPENCODE_USERNAME=${OPENCODE_USERNAME:-opencode}
MESH_DEVICE_NAME=${MESH_DEVICE_NAME:-}
MESH_PYTHON=${MESH_PYTHON:-python3}
INSTANCE=${MESH_INSTANCE:-default}
[[ "$INSTANCE" =~ ^[A-Za-z0-9_-]+$ ]] || exit 2
[[ "$INSTALL_DIR" == /* && "$INSTALL_DIR" != *"'"* && "$INSTALL_DIR" != *$'\n'* ]] || exit 2

case "$MESH_GATEWAY_URL" in
  https://*) ;;
  http://) echo "Gateway URL must include a host" >&2; exit 2 ;;
  http://*) echo "Gateway is not using HTTPS" >&2 ;;
  *) echo "Gateway URL must start with https:// or http://" >&2; exit 2 ;;
esac

command -v ssh >/dev/null || { echo 'ssh is required' >&2; exit 1; }
command -v tar >/dev/null || { echo 'tar is required' >&2; exit 1; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/config"

python3 - "$tmp/config/agent.local.json" <<'PY'
import json
import os
import sys

config = {
    "gateway_url": os.environ["MESH_GATEWAY_URL"].rstrip("/"),
    "enroll_token": os.environ["MESH_ENROLL_TOKEN"],
    "opencode_url": os.environ["OPENCODE_URL"].rstrip("/"),
    "reconnect_seconds": 5,
}
if config["gateway_url"].startswith("http://"):
    config["allow_insecure_gateway"] = True
if os.environ.get("OPENCODE_USERNAME") or os.environ.get("OPENCODE_PASSWORD"):
    config["opencode_basic_auth"] = {
        "username": os.environ.get("OPENCODE_USERNAME", "opencode"),
        "password": os.environ.get("OPENCODE_PASSWORD", ""),
    }
if os.environ.get("MESH_DEVICE_NAME"):
    config["device_name"] = os.environ["MESH_DEVICE_NAME"]
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(config, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY

echo "deploying OpenCode Mesh Agent to ${REMOTE}:${INSTALL_DIR}"
NEEDS_BOOTSTRAP=$(ssh -o BatchMode=yes "$REMOTE" "if [ -d '$INSTALL_DIR/src' ]; then echo 0; else echo 1; fi")
ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$REMOTE" \
  "mkdir -p '$INSTALL_DIR'"
if [[ "$NEEDS_BOOTSTRAP" == 1 ]]; then
tar -czf - --exclude='./.git' --exclude='./.venv' --exclude='./data' \
  --exclude='*.local.json' --exclude='./config/agents.json' src scripts pyproject.toml | \
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$REMOTE" \
  "tar -xzf - -C '$INSTALL_DIR'"
fi
ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$REMOTE" \
  "mkdir -p '$INSTALL_DIR/config'; install -m 0600 /dev/stdin '$INSTALL_DIR/config/.agent-incoming-$INSTANCE.json'" < "$tmp/config/agent.local.json"
ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$REMOTE" \
  "INSTALL_DIR='$INSTALL_DIR'" "PYTHON='$MESH_PYTHON'" "INSTANCE='$INSTANCE'" "INSTALL_ONLY='${MESH_INSTALL_ONLY:-0}'" "NEEDS_BOOTSTRAP='$NEEDS_BOOTSTRAP'" " bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail
SERVICE=opencode-mesh-agent
[[ "$INSTANCE" == default ]] || SERVICE="opencode-mesh-agent@$INSTANCE"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
[[ ! -e "$UNIT_DIR/$SERVICE.service" ]] || { echo 'Service already installed' >&2; exit 1; }
mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/config"
mkdir -p "$UNIT_DIR"
python3 - "$INSTALL_DIR" "$INSTANCE" <<'PY'
import json, os, sys, tempfile
from pathlib import Path
root, name = Path(sys.argv[1]), sys.argv[2]
incoming = root / "config" / f".agent-incoming-{name}.json"
path = root / "config" / "agents.json"
try:
    if not path.exists() and any((root / "config" / x).exists() for x in ("agent.json", "agent.local.json")):
        raise SystemExit("Migrate legacy configuration first")
    entry = json.loads(incoming.read_text())
    shared = json.loads(path.read_text()) if path.exists() else {"agents": {}}
    if name in shared["agents"]:
        raise SystemExit("Instance already configured")
    for key in ("gateway_url", "enroll_token"):
        value = entry.pop(key)
        if key in shared and shared[key] != value:
            raise SystemExit("Shared Gateway settings differ")
        shared[key] = value
    shared["agents"][name] = entry
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, "w") as handle:
        json.dump(shared, handle, indent=2)
    os.replace(temporary, path)
finally:
    incoming.unlink(missing_ok=True)
PY
if [[ "$NEEDS_BOOTSTRAP" == 1 ]]; then
if [ ! -d "$INSTALL_DIR/.venv" ]; then
  "$PYTHON" -m venv "$INSTALL_DIR/.venv" || "$PYTHON" -m venv --system-site-packages "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip
"$INSTALL_DIR/.venv/bin/python" -m pip install -e "$INSTALL_DIR"
fi
[[ -x "$INSTALL_DIR/.venv/bin/python" ]] || exit 1
cat > "$UNIT_DIR/$SERVICE.service" <<UNIT
[Unit]
Description=OpenCode Mesh Agent $INSTANCE
After=network-online.target
[Service]
WorkingDirectory=$INSTALL_DIR
ExecStart="$INSTALL_DIR/.venv/bin/python" -m src.main --mode agent --config "$INSTALL_DIR/config/agents.json" --instance $INSTANCE
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
if [[ "$INSTALL_ONLY" != 1 ]]; then
  loginctl enable-linger "$(id -un)" 2>/dev/null || true
  systemctl --user enable --now "$SERVICE.service"
  systemctl --user --no-pager --full status "$SERVICE.service"
fi
REMOTE_SCRIPT
echo "deployment complete: ${REMOTE}:${INSTALL_DIR}"
