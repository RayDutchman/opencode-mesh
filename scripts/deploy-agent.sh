#!/usr/bin/env bash
set -euo pipefail

# 一键把 Mesh Agent 部署到一台已经运行 OpenCode 的 Linux 设备。
# 凭据只通过环境变量传入，不写入仓库；远端配置写入 config/agent.local.json。

usage() {
  cat <<'EOF'
用法：
  MESH_GATEWAY_URL=https://oc.example.com \
  MESH_ENROLL_TOKEN=... \
  OPENCODE_URL=http://127.0.0.1:40960 \
  OPENCODE_USERNAME=opencode \
  OPENCODE_PASSWORD=... \
  scripts/deploy-agent.sh user@device [安装目录]

可选环境变量：
  MESH_DEVICE_NAME      设备显示名，默认使用远端 hostname
  MESH_INSTALL_DIR      默认 /opt/opencode-mesh
  MESH_PYTHON            默认 python3
  MESH_GATEWAY_URL      Gateway 公网地址，必填
  MESH_ENROLL_TOKEN     Gateway 注册令牌，必填
  OPENCODE_URL          本机 OpenCode 地址，必填
  OPENCODE_USERNAME     OpenCode Basic Auth 用户名，默认 opencode
  OPENCODE_PASSWORD     OpenCode Basic Auth 密码，可为空
EOF
}

[[ $# -ge 1 && $# -le 2 ]] || { usage >&2; exit 2; }
REMOTE=$1
INSTALL_DIR=${2:-${MESH_INSTALL_DIR:-/opt/opencode-mesh}}
: "${MESH_GATEWAY_URL:?必须设置 MESH_GATEWAY_URL}"
: "${MESH_ENROLL_TOKEN:?必须设置 MESH_ENROLL_TOKEN}"
: "${OPENCODE_URL:?必须设置 OPENCODE_URL}"
OPENCODE_USERNAME=${OPENCODE_USERNAME:-opencode}
MESH_DEVICE_NAME=${MESH_DEVICE_NAME:-}
MESH_PYTHON=${MESH_PYTHON:-python3}

command -v ssh >/dev/null || { echo '缺少 ssh' >&2; exit 1; }
command -v tar >/dev/null || { echo '缺少 tar' >&2; exit 1; }

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

echo "部署 OpenCode Mesh Agent 到 ${REMOTE}:${INSTALL_DIR}"
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
echo "部署完成：${REMOTE}:${INSTALL_DIR}"
