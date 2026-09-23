#!/usr/bin/env bash
set -euo pipefail

# Update an existing installation from a committed Git ref, preserving config/data.
# Usage: bash scripts/upgrade.sh HOST DIR agent|gateway user|system [REF]
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

  manager=(systemctl)
  [[ "$scope" != user ]] || manager+=(--user)

  # A shared-source upgrade targets the whole install directory: collect every Mesh
  # service unit under the same scope (default Agent, named Agent instances,
  # Gateway), then fold them into this directory by WorkingDirectory.
  # list-unit-files covers installed units regardless of enable state, list-units
  # covers running units; template units (@.service, no instance) are excluded
  # from discovery and guarding.
  declare -A candidates=()
  while read -r name _; do
    case "$name" in
      opencode-mesh-agent.service|opencode-mesh-agent@*.service|opencode-mesh-gateway.service)
        [[ "$name" == opencode-mesh-agent@.service ]] || candidates["$name"]=1
        ;;
    esac
  done < <("${manager[@]}" list-unit-files --no-legend --type=service 2>/dev/null || true)
  while read -r name _; do
    case "$name" in
      opencode-mesh-agent.service|opencode-mesh-agent@*.service|opencode-mesh-gateway.service)
        [[ "$name" == opencode-mesh-agent@.service ]] || candidates["$name"]=1
        ;;
    esac
  done < <("${manager[@]}" list-units --all --plain --no-legend --type=service 2>/dev/null || true)

  # WorkingDirectory must point at this install directory (compared after realpath
  # strips symlinks); units in other directories are always excluded.
  managed=()
  for name in "${!candidates[@]}"; do
    wd=$("${manager[@]}" show "$name" -p WorkingDirectory --value 2>/dev/null) || true
    if [[ -n "$wd" && "$(realpath "$wd" 2>/dev/null || true)" == "$root" ]]; then
      managed+=("$name")
    fi
  done
  mapfile -t managed < <(printf '%s\n' "${managed[@]}" | LC_ALL=C sort)

  # Refuse when the target role has no installed service, avoiding a silent
  # "deployed but nothing to update" success.
  role_found=0
  for name in "${managed[@]}"; do
    if [[ "$role" == gateway ]]; then
      if [[ "$name" == opencode-mesh-gateway.service ]]; then
        role_found=1
      fi
    elif [[ "$name" == opencode-mesh-agent.service || "$name" == opencode-mesh-agent@*.service ]]; then
      role_found=1
    fi
  done
  [[ "$role_found" == 1 ]] || {
    printf 'No installed %s service under %s (%s scope); refusing silent success.\n' \
      "$role" "$root" "$scope" >&2
    exit 1
  }

  # Record pre-upgrade run state: only units running before the upgrade are
  # stopped/restarted; originally inactive units stay stopped.
  declare -A was_active=()
  for name in "${managed[@]}"; do
    if "${manager[@]}" is-active --quiet "$name"; then
      was_active["$name"]=1
    else
      was_active["$name"]=0
    fi
  done

  if [[ -f "$root/.mesh-revision" && $(cat "$root/.mesh-revision") == "$revision" ]]; then
    printf 'Already at target commit %.12s; no upgrade or restart needed.\n' "$revision"
    exit 0
  fi
  # Recovery data exists only for this operation; on failure it is kept for manual handling.
  backup="$stage/rollback.tar.gz"
  files=(src scripts pyproject.toml)
  [[ ! -f "$root/.mesh-revision" ]] || files+=(.mesh-revision)
  tar -czf "$backup" -C "$root" "${files[@]}"

  source_changed=0
  rollback() {
    trap - ERR
    trap - EXIT
    recovery_failed=0
    printf 'Deployment failed; restoring source from %s\n' "$backup" >&2
    if [[ "$source_changed" == 1 ]]; then
      # The new version may have started only partially; stop these services again
      # before restoring source.
      if [[ ${#stop_targets[@]} -gt 0 ]]; then
        "${manager[@]}" stop "${stop_targets[@]}" || {
          printf 'Cannot safely restore running services; backup: %s\n' "$backup" >&2
          exit 1
        }
      fi
      rm -rf -- "$root/src" "$root/scripts" || { printf 'Restore failed; recovery data kept at %s\n' "$stage" >&2; exit 1; }
      rm -f -- "$root/.mesh-revision"
      tar -xzf "$backup" -C "$root" || { printf 'Restore failed; recovery data kept at %s\n' "$stage" >&2; exit 1; }
      "$python" -m pip install -e "$root" --quiet || recovery_failed=1
    fi
    # Restore every service that ran before the upgrade; units still running after
    # a partial stop are restarted too, so the original active set is fully
    # restored.
    for name in "${managed[@]}"; do
      if [[ "${was_active[$name]}" == 1 ]]; then
        "${manager[@]}" restart "$name" || recovery_failed=1
        "${manager[@]}" is-active --quiet "$name" || recovery_failed=1
      fi
    done
    [[ "$recovery_failed" == 0 ]] || { printf 'Recovery incomplete; recovery data kept at %s\n' "$stage" >&2; exit 1; }
    rm -rf -- "$stage"
    printf 'Restored the original version and running services.\n' >&2
    exit 1
  }
  trap rollback ERR

  stop_targets=()
  for name in "${managed[@]}"; do
    if [[ "${was_active[$name]}" == 1 ]]; then
      stop_targets+=("$name")
    fi
  done
  if [[ ${#stop_targets[@]} -gt 0 ]]; then
    # Stop all originally running units in one call; any failure enters rollback,
    # which restores the original active set as a whole.
    "${manager[@]}" stop "${stop_targets[@]}"
  fi

  # Install only after the complete archive has arrived and passed validation.
  source_changed=1
  rm -rf -- "$root/src" "$root/scripts"
  cp -a "$stage/src" "$stage/scripts" "$stage/pyproject.toml" "$root/"
  "$python" -m pip install -e "$root" --quiet
  printf '%s\n' "$revision" > "$root/.mesh-revision"
  if [[ ${#stop_targets[@]} -gt 0 ]]; then
    "${manager[@]}" restart "${stop_targets[@]}"
  fi
  # The health-check sleep window can be overridden with MESH_DEPLOY_HEALTH_SLEEP
  # (tests set it to 0; the default preserves the original behavior).
  sleep "${MESH_DEPLOY_HEALTH_SLEEP:-2}"
  for name in "${stop_targets[@]}"; do
    "${manager[@]}" is-active --quiet "$name"
  done
  trap - ERR
  printf 'Upgrade succeeded (%.12s); running services restored: %s\n' "$revision" "${stop_targets[*]:-none (no services were running)}"
  exit 0
fi

if [[ $# == 0 ]] && { : >/dev/tty; } 2>/dev/null; then
  ask() {
    local value
    printf '%s [%s]: ' "$1" "$2" >&2
    read -r value </dev/tty || return 1
    printf '%s' "${value:-$2}"
  }
  declare -A found=() services=() roles=()
  keys=()
  for scope in user system; do
    manager=(systemctl)
    [[ "$scope" != user ]] || manager+=(--user)
    while read -r name _; do
      case "$name" in
        opencode-mesh-agent.service|opencode-mesh-agent@*.service|opencode-mesh-gateway.service) ;;
        *) continue ;;
      esac
      [[ "$name" != opencode-mesh-agent@.service ]] || continue
      wd=$("${manager[@]}" show "$name" -p WorkingDirectory --value 2>/dev/null) || continue
      [[ -d "$wd" ]] || continue
      wd=$(realpath "$wd")
      key="$scope|$wd"
      if [[ -z "${found[$key]:-}" ]]; then
        found[$key]=1; keys+=("$key")
        roles[$key]=agent
        [[ "$name" != opencode-mesh-gateway.service ]] || roles[$key]=gateway
      fi
      services[$key]="${services[$key]:-} $name"
    done < <({ "${manager[@]}" list-unit-files --no-legend --type=service 2>/dev/null || true; "${manager[@]}" list-units --all --plain --no-legend --type=service 2>/dev/null || true; } | LC_ALL=C sort -u)
  done
  [[ ${#keys[@]} -gt 0 ]] || { printf 'No local Mesh installation found; run install.sh first, or pass a remote installation via positional arguments.\n' >&2; exit 1; }
  choice=1
  if [[ ${#keys[@]} -gt 1 ]]; then
    for i in "${!keys[@]}"; do printf '%s) %s\n' "$((i+1))" "${keys[$i]}"; done
    while true; do
      choice=$(ask 'Select an installation' 1)
      [[ "$choice" =~ ^[1-9][0-9]{0,5}$ ]] && ((choice<=${#keys[@]})) && break
      printf 'Invalid number; please choose again.\n' >&2
    done
  fi
  key=${keys[$((choice-1))]}; scope=${key%%|*}; root=${key#*|}; role=${roles[$key]}
  [[ -x "$root/.venv/bin/python" && -d "$root/src" && -d "$root/scripts" && -f "$root/pyproject.toml" ]] || { printf 'The discovered service directory is not a valid Mesh installation: %s; please check the service configuration.\n' "$root" >&2; exit 1; }
  host=local; ref=HEAD
  repo=$(git -C "$(dirname "$(realpath "$0")")" rev-parse --show-toplevel)
  target=$(git -C "$repo" rev-parse HEAD)
  current=$(cat "$root/.mesh-revision" 2>/dev/null || true)
  if [[ "$current" == "$target" ]]; then printf 'Already at current source commit %.12s; no upgrade or restart needed.\n' "$target"; exit 0; fi
  [[ "$scope" != system || $EUID == 0 ]] || { printf 'This is a system-scope installation; run the upgrade with sudo and explicit positional arguments.\n' >&2; exit 1; }
  version=$(git -C "$repo" show HEAD:src/__init__.py | sed -n 's/^__version__ = "\(.*\)"/\1/p')
  current_version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/src/__init__.py" 2>/dev/null || true)
  printf 'Current version: %s\n' "${current_version:-unknown}"
  printf 'Install location: %s\nService scope: %s\nCurrent commit: %.12s\nTarget version: %s (%.12s, current source repo)\nServices: %s\nUpgrade restarts only previously running services and preserves config and identity.\n' "$root" "$scope" "${current:-unknown}" "$version" "$target" "${services[$key]}"
  while true; do
    answer=$(ask 'Proceed with upgrade? (Y/n)' Y)
    case "$answer" in y|Y) break ;; n|N) printf 'Cancelled.\n'; exit 0 ;; *) printf 'Please enter y or n.\n' >&2 ;; esac
  done
  set -- "$host" "$root" "$role" "$scope" "$ref"
fi

[[ $# -ge 4 && $# -le 5 ]] || {
  printf 'Usage: bash %s HOST DIR agent|gateway user|system [TAG_OR_COMMIT]\n' "$0" >&2
  exit 2
}
host=$1 root=$2 role=$3 scope=$4 ref=${5:-HEAD}
case "$host" in localhost|127.0.0.1|::1) host=local ;; esac
if [[ "$host" == local ]]; then
  case "$root" in '~') root=$HOME ;; '~/'*) root="$HOME/${root:2}" ;; esac
  root=$(realpath -m "$root")
fi
[[ "$root" == /* && "$root" != / ]] || { printf 'Install directory must be an absolute path (remote paths are not ~-expanded).\n' >&2; exit 2; }
[[ "$role" == agent || "$role" == gateway ]] || { printf 'Role must be agent or gateway.\n' >&2; exit 2; }
[[ "$scope" == user || "$scope" == system ]] || { printf 'Scope is a service level and must be user or system, not a username.\n' >&2; exit 2; }
script=$(realpath "$0")
repo=$(git -C "$(dirname "$script")" rev-parse --show-toplevel)
revision=$(git -C "$repo" rev-parse --verify "${ref}^{commit}" 2>/dev/null) || { printf 'Git reference not found: %s; use HEAD for the current repo commit, or specify an existing release tag (e.g. v0.3.0).\n' "$ref" >&2; exit 2; }
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
    "$remote_tmp/upgrade.sh" "$remote_tmp/release.tar.gz" "$digest" "$root" "$role" "$scope" "$revision"
  ssh "${options[@]}" "$host" "$command"
fi
