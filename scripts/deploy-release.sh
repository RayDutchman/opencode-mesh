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

  manager=(systemctl)
  [[ "$scope" != user ]] || manager+=(--user)

  # 共享源码升级是整个安装目录的操作：收集同 scope 下所有 Mesh 服务单元
  # （默认 Agent、具名 Agent 实例、Gateway），随后用 WorkingDirectory 归并到本目录。
  # list-unit-files 覆盖 enabled/disabled 的已安装单元，list-units 覆盖正在运行的单元；
  # 模板单元（@.service，无实例）不参与发现与守卫。
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

  # WorkingDirectory 必须是本安装目录（realpath 消除符号链接后比较），其他目录的单元一律排除。
  managed=()
  for name in "${!candidates[@]}"; do
    wd=$("${manager[@]}" show "$name" -p WorkingDirectory --value 2>/dev/null) || true
    if [[ -n "$wd" && "$(realpath "$wd" 2>/dev/null || true)" == "$root" ]]; then
      managed+=("$name")
    fi
  done
  mapfile -t managed < <(printf '%s\n' "${managed[@]}" | LC_ALL=C sort)

  # 目标角色没有任何已安装服务时拒绝，避免“部署成功但没有对应服务可更新”的静默成功。
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

  # 记录升级前的运行状态：只 stop/restart 升级前运行的单元，原 inactive 保持停止。
  declare -A was_active=()
  for name in "${managed[@]}"; do
    if "${manager[@]}" is-active --quiet "$name"; then
      was_active["$name"]=1
    else
      was_active["$name"]=0
    fi
  done

  # Keep an on-host rollback archive, including the previous deployment identity.
  mkdir -p "$root/.mesh-backups"
  backup=$(mktemp "$root/.mesh-backups/source.XXXXXX.tar.gz")
  files=(src scripts pyproject.toml)
  [[ ! -f "$root/.mesh-revision" ]] || files+=(.mesh-revision)
  tar -czf "$backup" -C "$root" "${files[@]}"

  source_changed=0
  rollback() {
    trap - ERR
    printf 'Deployment failed; restoring source from %s\n' "$backup" >&2
    if [[ "$source_changed" == 1 ]]; then
      # 启动新版本可能只成功了一部分，恢复源码前必须再次停止这些服务。
      if [[ ${#stop_targets[@]} -gt 0 ]]; then
        "${manager[@]}" stop "${stop_targets[@]}" || {
          printf 'Cannot safely restore running services; backup: %s\n' "$backup" >&2
          exit 1
        }
      fi
      rm -rf -- "$root/src" "$root/scripts"
      rm -f -- "$root/.mesh-revision"
      tar -xzf "$backup" -C "$root"
      "$python" -m pip install -e "$root" --quiet || true
    fi
    # 恢复所有升级前运行的服务；部分 stop 失败时仍在运行的单元同样重启，
    # 保证原 active 集合整体恢复原状。
    for name in "${managed[@]}"; do
      if [[ "${was_active[$name]}" == 1 ]]; then
        "${manager[@]}" restart "$name" || true
      fi
    done
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
    # 一次调用停掉全部原运行单元；任一失败进入回滚，回滚按原 active 集合整体恢复。
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
  # 健康检查等待窗口可用 MESH_DEPLOY_HEALTH_SLEEP 覆盖（测试置 0，默认保持原行为）。
  sleep "${MESH_DEPLOY_HEALTH_SLEEP:-2}"
  for name in "${stop_targets[@]}"; do
    "${manager[@]}" is-active --quiet "$name"
  done
  trap - ERR
  printf 'Deployed %s: restarted %s (%s); source backup: %s\n' "$revision" "${stop_targets[*]}" "$root" "$backup"
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
