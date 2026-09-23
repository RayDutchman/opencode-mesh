#!/usr/bin/env bash
set -euo pipefail

# Update an existing installation from a committed Git ref, preserving config/data.
# 用法：bash scripts/upgrade.sh HOST DIR agent|gateway user|system [REF]
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

  if [[ -f "$root/.mesh-revision" && $(cat "$root/.mesh-revision") == "$revision" ]]; then
    printf '已是目标提交 %.12s，无需升级或重启。\n' "$revision"
    exit 0
  fi
  # 恢复资料仅在本次操作期间存在；恢复失败时保留供人工处理。
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
      # 启动新版本可能只成功了一部分，恢复源码前必须再次停止这些服务。
      if [[ ${#stop_targets[@]} -gt 0 ]]; then
        "${manager[@]}" stop "${stop_targets[@]}" || {
          printf 'Cannot safely restore running services; backup: %s\n' "$backup" >&2
          exit 1
        }
      fi
      rm -rf -- "$root/src" "$root/scripts" || { printf '恢复失败，资料保留于 %s\n' "$stage" >&2; exit 1; }
      rm -f -- "$root/.mesh-revision"
      tar -xzf "$backup" -C "$root" || { printf '恢复失败，资料保留于 %s\n' "$stage" >&2; exit 1; }
      "$python" -m pip install -e "$root" --quiet || recovery_failed=1
    fi
    # 恢复所有升级前运行的服务；部分 stop 失败时仍在运行的单元同样重启，
    # 保证原 active 集合整体恢复原状。
    for name in "${managed[@]}"; do
      if [[ "${was_active[$name]}" == 1 ]]; then
        "${manager[@]}" restart "$name" || recovery_failed=1
        "${manager[@]}" is-active --quiet "$name" || recovery_failed=1
      fi
    done
    [[ "$recovery_failed" == 0 ]] || { printf '恢复未完全成功，资料保留于 %s\n' "$stage" >&2; exit 1; }
    rm -rf -- "$stage"
    printf '已恢复原版本和原运行服务。\n' >&2
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
  printf '升级成功（%.12s），已恢复运行服务：%s\n' "$revision" "${stop_targets[*]:-无（原服务均未运行）}"
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
  [[ ${#keys[@]} -gt 0 ]] || { printf '未发现本机 Mesh 安装；请先运行 install.sh，或通过位置参数指定远程安装。\n' >&2; exit 1; }
  choice=1
  if [[ ${#keys[@]} -gt 1 ]]; then
    for i in "${!keys[@]}"; do printf '%s) %s\n' "$((i+1))" "${keys[$i]}"; done
    while true; do
      choice=$(ask '请选择安装编号' 1)
      [[ "$choice" =~ ^[1-9][0-9]{0,5}$ ]] && ((choice<=${#keys[@]})) && break
      printf '编号无效，请重新选择。\n' >&2
    done
  fi
  key=${keys[$((choice-1))]}; scope=${key%%|*}; root=${key#*|}; role=${roles[$key]}
  [[ -x "$root/.venv/bin/python" && -d "$root/src" && -d "$root/scripts" && -f "$root/pyproject.toml" ]] || { printf '发现的服务目录不是有效的 Mesh 安装：%s；请检查服务配置。\n' "$root" >&2; exit 1; }
  host=local; ref=HEAD
  repo=$(git -C "$(dirname "$(realpath "$0")")" rev-parse --show-toplevel)
  target=$(git -C "$repo" rev-parse HEAD)
  current=$(cat "$root/.mesh-revision" 2>/dev/null || true)
  if [[ "$current" == "$target" ]]; then printf '已是当前源码提交 %.12s，无需升级或重启。\n' "$target"; exit 0; fi
  [[ "$scope" != system || $EUID == 0 ]] || { printf '这是系统级安装，请使用 sudo 及明确位置参数执行升级。\n' >&2; exit 1; }
  version=$(git -C "$repo" show HEAD:src/__init__.py | sed -n 's/^__version__ = "\(.*\)"/\1/p')
  current_version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/src/__init__.py" 2>/dev/null || true)
  printf '当前版本：%s\n' "${current_version:-未知}"
  printf '安装位置：%s\n服务作用域：%s\n当前提交：%.12s\n目标版本：%s（%.12s，当前源码仓库）\n关联服务：%s\n升级仅重启原先运行的服务，保留配置与身份。\n' "$root" "$scope" "${current:-未知}" "$version" "$target" "${services[$key]}"
  while true; do
    answer=$(ask '是否升级？(Y/n)' Y)
    case "$answer" in y|Y) break ;; n|N) printf '已取消。\n'; exit 0 ;; *) printf '请输入 y 或 n。\n' >&2 ;; esac
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
[[ "$root" == /* && "$root" != / ]] || { printf '安装目录必须为绝对路径（远程路径不展开 ~）。\n' >&2; exit 2; }
[[ "$role" == agent || "$role" == gateway ]] || { printf '角色只能是 agent 或 gateway。\n' >&2; exit 2; }
[[ "$scope" == user || "$scope" == system ]] || { printf 'Scope 是服务层级，只能是 user 或 system，不是用户名。\n' >&2; exit 2; }
script=$(realpath "$0")
repo=$(git -C "$(dirname "$script")" rev-parse --show-toplevel)
revision=$(git -C "$repo" rev-parse --verify "${ref}^{commit}" 2>/dev/null) || { printf '找不到 Git 引用 %s；使用 HEAD 表示当前仓库提交，或指定已有发布标签（例如 v0.3.0）。\n' "$ref" >&2; exit 2; }
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
