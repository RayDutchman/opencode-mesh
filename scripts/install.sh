#!/usr/bin/env bash
set -euo pipefail

# OpenCode Mesh installer (run on the target device)
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | bash
#   or with an explicit mode:
#   curl -fsSL .../install.sh | bash -s -- agent
#   curl -fsSL .../install.sh | bash -s -- gateway
#
# Non-interactive install via environment variables:
#   MESH_MODE=agent MESH_GATEWAY_URL=... MESH_ENROLL_TOKEN=... \
#   OPENCODE_URL=... OPENCODE_USERNAME=... OPENCODE_PASSWORD=... \
#   bash install.sh
#
# Gateway only: MESH_PUBLIC_URL prints a ready-to-paste Agent install command.
#
# Install from a local source directory (skips the GitHub download):
#   MESH_SOURCE_DIR=/path/to/opencode-mesh MESH_MODE=agent ... bash install.sh
# Install a release tag instead of the development branch:
#   MESH_VERSION=v0.1.0 bash install.sh agent

info() { printf '\033[1;34m[mesh]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[mesh]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[mesh]\033[0m %s\n' "$*" >&2; }

# Read prompts from the controlling terminal when one exists, so `curl | bash`
# still works even though stdin is the pipe.
TTY_AVAILABLE=""
if { : >/dev/tty; } 2>/dev/null; then
  TTY_AVAILABLE="/dev/tty"
fi

ask() {
  local prompt="$1" default="$2" value=""
  if [ -n "$TTY_AVAILABLE" ]; then
    if [ -n "$default" ]; then
      printf '%s [%s]: ' "$prompt" "$default" >&2
    else
      printf '%s: ' "$prompt" >&2
    fi
    read -r value < "$TTY_AVAILABLE" 2>/dev/null || value=""
  fi
  printf '%s' "${value:-$default}"
}

ask_secret() {
  local prompt="$1" default="$2" value=""
  if [ -n "$TTY_AVAILABLE" ]; then
    printf '%s: ' "$prompt" >&2
    read -rs value < "$TTY_AVAILABLE" 2>/dev/null || value=""
    printf '\n' >&2
  fi
  printf '%s' "${value:-$default}"
}

require_value() {
  local name="$1" value="$2"
  if [ -z "$value" ]; then
    err "${name} is required; provide it via an environment variable for non-interactive installs"
    exit 1
  fi
}

MODE="${1:-${MESH_MODE:-}}"

# Pick the systemd scope and install directory based on the running user.
if [ "$(id -u)" -eq 0 ]; then
  INSTALL_DIR="${MESH_INSTALL_DIR:-/opt/opencode-mesh}"
  SYSTEMD_KIND="system"
  UNIT_DIR="/etc/systemd/system"
else
  INSTALL_DIR="${MESH_INSTALL_DIR:-$HOME/.local/share/opencode-mesh}"
  SYSTEMD_KIND="user"
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
fi

command -v python3 >/dev/null 2>&1 || { err "python3 is required"; exit 1; }
if [ -z "${MESH_SOURCE_DIR:-}" ]; then
  command -v curl >/dev/null 2>&1 || { err "curl is required"; exit 1; }
  command -v tar  >/dev/null 2>&1 || { err "tar is required"; exit 1; }
fi

if [ -z "$MODE" ]; then
  printf 'Select install mode:\n'
  printf '  1) agent   runs on the OpenCode host and proxies the local OpenCode\n'
  printf '  2) gateway runs on a public server and handles auth and device routing\n'
  MODE="$(ask "Enter 1 or 2" "")"
  case "$MODE" in
    1|agent) MODE="agent" ;;
    2|gateway) MODE="gateway" ;;
    *) err "invalid choice; use MESH_MODE=agent|gateway or 'bash install.sh agent'"; exit 1 ;;
  esac
fi

if [ "$MODE" != "agent" ] && [ "$MODE" != "gateway" ]; then
  err "mode must be agent or gateway"; exit 1
fi

SERVICE_NAME="opencode-mesh-${MODE}"
VERSION="${MESH_VERSION:-main}"
if [[ "$VERSION" == v* ]]; then
  TARBALL="https://github.com/RayDutchman/opencode-mesh/archive/refs/tags/${VERSION}.tar.gz"
  SOURCE_KIND="release tag"
else
  TARBALL="https://github.com/RayDutchman/opencode-mesh/archive/refs/heads/${VERSION}.tar.gz"
  SOURCE_KIND="branch"
fi

info "install directory: ${INSTALL_DIR}"
info "systemd scope: ${SYSTEMD_KIND}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
if [ -n "${MESH_SOURCE_DIR:-}" ]; then
  SRC="${MESH_SOURCE_DIR%/}"
  info "using local source directory: ${SRC}"
  if [ ! -d "$SRC/src" ]; then
    err "MESH_SOURCE_DIR does not contain a src/ directory"; exit 1
  fi
else
  info "downloading opencode-mesh source (${VERSION}, ${SOURCE_KIND})..."
  curl -fsSL "$TARBALL" -o "$tmp/mesh.tar.gz"
  tar -xzf "$tmp/mesh.tar.gz" -C "$tmp"
  SRC="$(find "$tmp" -maxdepth 1 -type d -name 'opencode-mesh-*' | head -n 1)"
  if [ -z "$SRC" ] || [ ! -d "$SRC/src" ]; then
    err "source directory not found after extraction; the archive may be corrupt"; exit 1
  fi
fi

mkdir -p "$INSTALL_DIR"
if [ -d "$INSTALL_DIR/src" ]; then
  warn "existing install detected; overwriting source and scripts (config and data are kept)"
  rm -rf "$INSTALL_DIR/src" "$INSTALL_DIR/scripts" "$INSTALL_DIR/pyproject.toml"
fi
cp -r "$SRC"/src "$SRC"/pyproject.toml "$SRC"/scripts "$INSTALL_DIR"/

if [ ! -d "$INSTALL_DIR/.venv" ]; then
  info "creating Python virtual environment..."
  python3 -m venv "$INSTALL_DIR/.venv" 2>/dev/null || python3 -m venv --system-site-packages "$INSTALL_DIR/.venv"
fi

info "installing dependencies (first run may take a while; aiortc needs a wheel or build)..."
"$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip -q
"$INSTALL_DIR/.venv/bin/python" -m pip install -e "$INSTALL_DIR" -q

mkdir -p "$INSTALL_DIR/config" "$INSTALL_DIR/data"
chmod 700 "$INSTALL_DIR/config" "$INSTALL_DIR/data"
chmod 600 "$INSTALL_DIR"/config/*.json 2>/dev/null || true
chmod 600 "$INSTALL_DIR"/data/*.json 2>/dev/null || true

info "configuring ${MODE} ..."
if [ "$MODE" = "agent" ]; then
  GATEWAY_URL="$(ask "Gateway public URL (required)" "${MESH_GATEWAY_URL:-}")"
  require_value "Gateway URL" "$GATEWAY_URL"
  ALLOW_INSECURE=""
  case "$GATEWAY_URL" in
    https://*) ;;
    http://*)
      warn "Gateway is not using HTTPS; enrollment token and proxied data will be sent in clear text"
      ALLOW_INSECURE="1"
      ;;
    *) err "Gateway URL must start with https:// or http://"; exit 1 ;;
  esac
  ENROLL_TOKEN="$(ask_secret "enroll_token (required)" "${MESH_ENROLL_TOKEN:-}")"
  require_value "enroll_token" "$ENROLL_TOKEN"
  OPENCODE_URL="$(ask "Local OpenCode URL (with port)" "${OPENCODE_URL:-http://127.0.0.1:4096}")"
  OPENCODE_USERNAME="$(ask "OpenCode username (leave empty if auth is disabled)" "${OPENCODE_USERNAME:-}")"
  OPENCODE_PASSWORD="$(ask_secret "OpenCode password (leave empty if auth is disabled)" "${OPENCODE_PASSWORD:-}")"
  CONFIG_FILE="$INSTALL_DIR/config/agent.json"
  INSTALL_DIR="$INSTALL_DIR" GATEWAY_URL="$GATEWAY_URL" ENROLL_TOKEN="$ENROLL_TOKEN" \
    OPENCODE_URL="$OPENCODE_URL" OPENCODE_USERNAME="$OPENCODE_USERNAME" OPENCODE_PASSWORD="$OPENCODE_PASSWORD" \
    ALLOW_INSECURE="$ALLOW_INSECURE" \
    python3 - "$CONFIG_FILE" <<'PY'
import json, os, sys
cfg = {
    "gateway_url": os.environ["GATEWAY_URL"].rstrip("/"),
    "enroll_token": os.environ["ENROLL_TOKEN"],
    "opencode_url": os.environ["OPENCODE_URL"].rstrip("/"),
    "state_file": os.path.join(os.environ["INSTALL_DIR"], "data", "agent-state.json"),
    "reconnect_seconds": 5,
}
if os.environ.get("ALLOW_INSECURE"):
    cfg["allow_insecure_gateway"] = True
if os.environ.get("OPENCODE_PASSWORD"):
    cfg["opencode_basic_auth"] = {
        "username": os.environ.get("OPENCODE_USERNAME") or "opencode",
        "password": os.environ["OPENCODE_PASSWORD"],
    }
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
os.chmod(sys.argv[1], 0o600)
PY
  EXEC="\"$INSTALL_DIR/.venv/bin/python\" -m src.main --mode agent --config \"$CONFIG_FILE\""
else
  LISTEN_PORT="$(ask "Listen port" "${MESH_LISTEN_PORT:-18080}")"
  case "$LISTEN_PORT" in
    ''|*[!0-9]*) err "listen port must be a number"; exit 1 ;;
  esac
  USERNAME="$(ask "Gateway login username (required)" "${MESH_USERNAME:-}")"
  require_value "Gateway login username (set MESH_USERNAME)" "$USERNAME"
  PASSWORD="$(ask_secret "Login password (required)" "${MESH_PASSWORD:-}")"
  require_value "Login password" "$PASSWORD"
  ENROLL_TOKEN="$(ask "enroll_token (empty to generate)" "${MESH_ENROLL_TOKEN:-}")"
  if [ -z "$ENROLL_TOKEN" ]; then
    ENROLL_TOKEN="$("$INSTALL_DIR/.venv/bin/python" -c 'import secrets;print(secrets.token_urlsafe(32))')"
  fi
  PUBLIC_URL="$(ask "Gateway public URL (used to print the agent command, optional)" "${MESH_PUBLIC_URL:-}")"
  CONFIG_FILE="$INSTALL_DIR/config/gateway.json"
  INSTALL_DIR="$INSTALL_DIR" LISTEN_PORT="$LISTEN_PORT" USERNAME="$USERNAME" PASSWORD="$PASSWORD" ENROLL_TOKEN="$ENROLL_TOKEN" \
    python3 - "$CONFIG_FILE" <<'PY'
import json, os, sys
cfg = {
    "listen_host": "127.0.0.1",
    "listen_port": int(os.environ["LISTEN_PORT"]),
    "state_file": os.path.join(os.environ["INSTALL_DIR"], "data", "gateway-state.json"),
    "enroll_token": os.environ["ENROLL_TOKEN"],
    "default_device": "",
    "auth": {"username": os.environ["USERNAME"], "password": os.environ["PASSWORD"]},
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
os.chmod(sys.argv[1], 0o600)
PY
  EXEC="\"$INSTALL_DIR/.venv/bin/python\" -m src.main --mode gateway --config \"$CONFIG_FILE\""
fi

info "writing systemd unit ..."
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
  info "enabling user linger (keeps the service running while logged out)..."
  loginctl enable-linger "$(id -un)" 2>/dev/null || warn "could not enable linger (no loginctl); the session must stay logged in"
  systemctl --user daemon-reload
  systemctl --user enable --now "${SERVICE_NAME}.service"
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

info "starting service..."
if [ "$SYSTEMD_KIND" = "user" ]; then
  systemctl --user restart "${SERVICE_NAME}.service"
  systemctl --user --no-pager --full status "${SERVICE_NAME}.service" || true
else
  systemctl restart "${SERVICE_NAME}.service"
  systemctl --no-pager --full status "${SERVICE_NAME}.service" || true
fi

INSTALLED_VERSION="$(cd "$INSTALL_DIR" && "$INSTALL_DIR/.venv/bin/python" -c 'import src; print(src.__version__)')"
info "install complete (OpenCode Mesh v${INSTALLED_VERSION})."

if [ "$MODE" = "gateway" ]; then
  SHOWN_URL="${PUBLIC_URL:-<your-gateway-url>}"
  cat <<INFO

========================================================================
Gateway is ready.

  Public URL  : ${SHOWN_URL}
  Login user  : ${USERNAME}
  enroll_token: ${ENROLL_TOKEN}

  Save the enroll_token. Every Agent needs it to join this Gateway.

  Install an Agent on each OpenCode device:

    read -r -s MESH_ENROLL_TOKEN; export MESH_ENROLL_TOKEN
    curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/install.sh | MESH_GATEWAY_URL=${SHOWN_URL} bash -s -- agent

========================================================================
INFO
fi

info "uninstall: curl -fsSL https://raw.githubusercontent.com/RayDutchman/opencode-mesh/main/scripts/uninstall.sh | bash -s -- ${MODE}"
