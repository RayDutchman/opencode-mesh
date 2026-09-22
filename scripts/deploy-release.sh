#!/usr/bin/env bash
set -euo pipefail

# Update an existing installation from a committed Git ref, preserving config/data.
# Usage: bash scripts/deploy-release.sh HOST DIR agent|gateway user|system [REF]
# HOST=local runs the same deployment steps without SSH. REF defaults to HEAD.

if [[ "${1:-}" == --apply ]]; then
  shift
  [[ $# == 6 ]] || exit 2
  archive=$1 digest=$2 root=$3 role=$4 scope=$5 revision=$6
  [[ "$root" == /* && "$root" != / ]] || exit 2
  [[ "$role" == agent || "$role" == gateway ]] || exit 2
  [[ "$scope" == user || "$scope" == system ]] || exit 2
  root=$(realpath "$root")
  python="$root/.venv/bin/python"
  [[ -x "$python" && -d "$root/src" && -d "$root/scripts" && -f "$root/pyproject.toml" ]] || {
    printf 'Existing installation required: %s\n' "$root" >&2; exit 1;
  }
  printf '%s  %s\n' "$digest" "$archive" | sha256sum --check --status
  stage=$(mktemp -d "$root/.mesh-stage.XXXXXX")
  trap 'rm -rf -- "$stage"' EXIT
  tar -xzf "$archive" -C "$stage"
  (cd "$stage" && "$python" -c 'import src.main; from src import __version__; print("Deploying Mesh", __version__)')

  # Keep an on-host rollback archive, including the previous deployment identity.
  mkdir -p "$root/.mesh-backups"
  backup=$(mktemp "$root/.mesh-backups/source.XXXXXX.tar.gz")
  files=(src scripts pyproject.toml)
  [[ ! -f "$root/.mesh-revision" ]] || files+=(.mesh-revision)
  tar -czf "$backup" -C "$root" "${files[@]}"
  service="opencode-mesh-${role}.service"
  manager=(systemctl)
  [[ "$scope" != user ]] || manager+=(--user)
  "${manager[@]}" stop "$service"
  rollback() {
    trap - ERR
    printf 'Deployment failed; restoring source from %s\n' "$backup" >&2
    rm -rf -- "$root/src" "$root/scripts"
    rm -f -- "$root/.mesh-revision"
    tar -xzf "$backup" -C "$root"
    "$python" -m pip install -e "$root" --quiet || true
    "${manager[@]}" restart "$service" || true
    exit 1
  }
  trap rollback ERR
  # Install only after the complete archive has arrived and passed validation.
  rm -rf -- "$root/src" "$root/scripts"
  cp -a "$stage/src" "$stage/scripts" "$stage/pyproject.toml" "$root/"
  "$python" -m pip install -e "$root" --quiet
  printf '%s\n' "$revision" > "$root/.mesh-revision"
  "${manager[@]}" restart "$service"
  sleep 2
  "${manager[@]}" is-active --quiet "$service"
  trap - ERR
  printf 'Deployed %s: %s (%s); source backup: %s\n' "$revision" "$service" "$root" "$backup"
  exit 0
fi

[[ $# -ge 4 && $# -le 5 ]] || {
  printf 'Usage: bash %s HOST DIR agent|gateway user|system [TAG_OR_COMMIT]\n' "$0" >&2
  exit 2
}
host=$1 root=$2 role=$3 scope=$4 ref=${5:-HEAD}
[[ "$root" == /* && "$root" != / ]] || exit 2
[[ "$role" == agent || "$role" == gateway ]] || exit 2
[[ "$scope" == user || "$scope" == system ]] || exit 2
script=$(realpath "$0")
repo=$(git -C "$(dirname "$script")" rev-parse --show-toplevel)
revision=$(git -C "$repo" rev-parse --verify "${ref}^{commit}")
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
git -C "$repo" archive --format=tar.gz "$revision" src scripts pyproject.toml > "$tmp/release.tar.gz"
digest=$(sha256sum "$tmp/release.tar.gz")
digest=${digest%% *}
printf 'Deploying committed revision %s to %s:%s\n' "$revision" "$host" "$root"

if [[ "$host" == local ]]; then
  # Never replace a developer checkout with older code or discard working changes.
  if [[ -e "$root/.git" ]]; then
    [[ $(git -C "$root" rev-parse HEAD) == "$revision" && -z $(git -C "$root" status --porcelain) ]] || {
      printf 'Local checkout must be clean and checked out at the requested ref.\n' >&2; exit 1;
    }
  fi
  cp "$script" "$tmp/deploy.sh"
  bash "$tmp/deploy.sh" --apply "$tmp/release.tar.gz" "$digest" "$root" "$role" "$scope" "$revision"
else
  options=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=10 -o ServerAliveCountMax=3)
  remote_tmp=$(ssh "${options[@]}" "$host" 'mktemp -d /tmp/opencode-mesh-deploy.XXXXXX')
  [[ "$remote_tmp" == /tmp/opencode-mesh-deploy.* && "$remote_tmp" != *$'\n'* ]] || exit 1
  cleanup() {
    ssh "${options[@]}" "$host" "rm -rf -- '$remote_tmp'" || true
    rm -rf -- "$tmp"
  }
  trap cleanup EXIT
  scp "${options[@]}" "$tmp/release.tar.gz" "$script" "$host:$remote_tmp/"
  printf -v command 'bash %q --apply %q %q %q %q %q %q' \
    "$remote_tmp/deploy-release.sh" "$remote_tmp/release.tar.gz" "$digest" "$root" "$role" "$scope" "$revision"
  ssh "${options[@]}" "$host" "$command"
fi
