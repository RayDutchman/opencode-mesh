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
    "listen_host": "127.0.0.1",
    "listen_port": 18090,
    "gateway_url": os.environ["MESH_GATEWAY_URL"].rstrip("/"),
    "enroll_token": os.environ["MESH_ENROLL_TOKEN"],
    "opencode_url": os.environ["OPENCODE_URL"].rstrip("/"),
    "state_file": "./data/agent-state.json",
    "reconnect_seconds": 5,
}
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
ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$REMOTE" \
  "mkdir -p '$INSTALL_DIR'"
tar -czf - --exclude='./.git' --exclude='./.venv' --exclude='./data' \
  --exclude='*.local.json' . | \
  ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$REMOTE" \
  "tar -xzf - -C '$INSTALL_DIR'"
ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$REMOTE" \
  "INSTALL_DIR='$INSTALL_DIR' PYTHON='$MESH_PYTHON' bash -s" <<'REMOTE_SCRIPT'
set -euo pipefail
mkdir -p "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/config"
mkdir -p "$HOME/.config/systemd/user"
if [ ! -d "$INSTALL_DIR/.venv" ]; then
  "$PYTHON" -m venv "$INSTALL_DIR/.venv" || "$PYTHON" -m venv --system-site-packages "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip
"$INSTALL_DIR/.venv/bin/python" -m pip install -e "$INSTALL_DIR"
install -m 0644 "$INSTALL_DIR/deploy/opencode-mesh-agent.service.example" \
  "$HOME/.config/systemd/user/opencode-mesh-agent.service"
sed -i "s#^WorkingDirectory=.*#WorkingDirectory=$INSTALL_DIR#; s#^ExecStart=.*#ExecStart=$INSTALL_DIR/.venv/bin/python -m src.main --mode agent --config $INSTALL_DIR/config/agent.local.json#; s#^ReadWritePaths=.*#ReadWritePaths=$INSTALL_DIR/data $INSTALL_DIR/config#" \
  "$HOME/.config/systemd/user/opencode-mesh-agent.service"
systemctl --user daemon-reload
systemctl --user enable --now opencode-mesh-agent.service
systemctl --user --no-pager --full status opencode-mesh-agent.service
REMOTE_SCRIPT

ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$REMOTE" \
  "cat > '$INSTALL_DIR/config/agent.local.json'" < "$tmp/config/agent.local.json"
ssh -o BatchMode=yes -o StrictHostKeyChecking=no "$REMOTE" \
  "systemctl --user restart opencode-mesh-agent.service"
echo "deployment complete: ${REMOTE}:${INSTALL_DIR}"
