#!/usr/bin/env bash
# Shared pool-ledger layout helpers for SC-node install/discovery scripts.
# Never print DATABASE_URL or other secrets.

set -euo pipefail

POOL_LEDGER_DIR="${POOL_LEDGER_DIR:-/etc/azcoin-super/pool-ledger}"
COLLECTOR_ENV="${COLLECTOR_ENV:-${POOL_LEDGER_DIR}/collector.env}"
FRESH_CYCLE_ENV="${FRESH_CYCLE_ENV:-${POOL_LEDGER_DIR}/fresh-cycle-automation.env}"
SCHEDULER_ENV="${SCHEDULER_ENV:-${POOL_LEDGER_DIR}/payout-scheduler.env}"

POOL_LEDGER_DIR_OWNER="${POOL_LEDGER_DIR_OWNER:-root}"
POOL_LEDGER_DIR_GROUP="${POOL_LEDGER_DIR_GROUP:-azledger}"
POOL_LEDGER_DIR_MODE="${POOL_LEDGER_DIR_MODE:-0750}"

COLLECTOR_ENV_MODE="${COLLECTOR_ENV_MODE:-0640}"
FRESH_CYCLE_ENV_MODE="${FRESH_CYCLE_ENV_MODE:-0640}"
SCHEDULER_ENV_MODE="${SCHEDULER_ENV_MODE:-0660}"

SECRET_KEY_PATTERN='(pass|password|token|secret|private|privkey|key|rpcauth|rpcpassword|database_url|url|cookie|bearer|auth|phrase|approval)'

pool_ledger_redact_line() {
  local line="$1"
  if [[ "${line}" =~ ^[[:space:]]*# ]] || [[ -z "${line//[[:space:]]/}" ]]; then
    printf '%s\n' "${line}"
    return 0
  fi
  local lower="${line,,}"
  if [[ "${lower}" =~ ${SECRET_KEY_PATTERN} ]]; then
    if [[ "${line}" == *"="* ]]; then
      printf '%s=<REDACTED>\n' "${line%%=*}"
    else
      printf '<REDACTED_LINE>\n'
    fi
    return 0
  fi
  printf '%s\n' "${line}"
}

pool_ledger_redact_file() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    echo "MISSING"
    return 0
  fi
  while IFS= read -r line || [[ -n "${line}" ]]; do
    pool_ledger_redact_line "${line}"
  done < "${path}"
}

pool_ledger_file_stat_summary() {
  local path="$1"
  if [[ ! -e "${path}" ]]; then
    echo "MISSING path=${path}"
    return 0
  fi
  stat -c 'PERMS=%a OWNER=%U GROUP=%G PATH=%n' "${path}"
}

pool_ledger_validate_dir_layout() {
  local errors=0
  if [[ ! -d "${POOL_LEDGER_DIR}" ]]; then
    echo "LAYOUT_ERROR missing directory ${POOL_LEDGER_DIR}"
    return 1
  fi
  local perms owner group
  perms="$(stat -c '%a' "${POOL_LEDGER_DIR}")"
  owner="$(stat -c '%U' "${POOL_LEDGER_DIR}")"
  group="$(stat -c '%G' "${POOL_LEDGER_DIR}")"
  if [[ "${owner}" != "${POOL_LEDGER_DIR_OWNER}" || "${group}" != "${POOL_LEDGER_DIR_GROUP}" ]]; then
    echo "LAYOUT_WARN directory owner/group=${owner}:${group} expected=${POOL_LEDGER_DIR_OWNER}:${POOL_LEDGER_DIR_GROUP}"
    errors=1
  fi
  if [[ "${perms}" != "${POOL_LEDGER_DIR_MODE}" ]]; then
    echo "LAYOUT_WARN directory mode=${perms} expected=${POOL_LEDGER_DIR_MODE}"
    errors=1
  fi
  return "${errors}"
}

pool_ledger_validate_env_file() {
  local path="$1"
  local expected_mode="$2"
  if [[ ! -f "${path}" ]]; then
    echo "ENV_MISSING path=${path}"
    return 0
  fi
  pool_ledger_file_stat_summary "${path}"
  local perms owner group
  perms="$(stat -c '%a' "${path}")"
  owner="$(stat -c '%U' "${path}")"
  group="$(stat -c '%G' "${path}")"
  if [[ "${owner}" != "root" || "${group}" != "azledger" ]]; then
    echo "ENV_WARN path=${path} owner/group=${owner}:${group} expected=root:azledger"
  fi
  if [[ "${perms}" != "${expected_mode}" ]]; then
    echo "ENV_WARN path=${path} mode=${perms} expected=${expected_mode}"
  fi
}

pool_ledger_ensure_layout() {
  install -d -o "${POOL_LEDGER_DIR_OWNER}" -g "${POOL_LEDGER_DIR_GROUP}" -m "${POOL_LEDGER_DIR_MODE}" \
    "${POOL_LEDGER_DIR}"
  chown "${POOL_LEDGER_DIR_OWNER}:${POOL_LEDGER_DIR_GROUP}" "${POOL_LEDGER_DIR}"
  chmod "${POOL_LEDGER_DIR_MODE}" "${POOL_LEDGER_DIR}"
}

pool_ledger_backup_file() {
  local path="$1"
  local backup_dir="${2:-${POOL_LEDGER_DIR}/backups}"
  if [[ ! -f "${path}" ]]; then
    return 0
  fi
  install -d -o root -g azledger -m 0750 "${backup_dir}"
  local timestamp base dest
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  base="$(basename "${path}")"
  dest="${backup_dir}/${base}.${timestamp}.bak"
  cp -a "${path}" "${dest}"
  chown root:azledger "${dest}"
  chmod 0640 "${dest}"
  echo "BACKUP_OK path=${dest}"
}

pool_ledger_install_env_if_missing() {
  local example="$1"
  local dest="$2"
  local mode="$3"
  if [[ -f "${dest}" ]]; then
    chown root:azledger "${dest}"
    chmod "${mode}" "${dest}"
    echo "ENV_SKIP exists path=${dest}"
    return 0
  fi
  if [[ ! -f "${example}" ]]; then
    echo "ENV_ERROR missing example ${example}" >&2
    return 1
  fi
  install -m "${mode}" -o root -g azledger "${example}" "${dest}"
  echo "ENV_INSTALL_OK path=${dest} mode=${mode}"
}

pool_ledger_redacted_diff() {
  local old_file="$1"
  local new_file="$2"
  if [[ ! -f "${old_file}" ]]; then
    echo "DIFF: (no previous file)"
    pool_ledger_redact_file "${new_file}"
    return 0
  fi
  diff -u "${old_file}" "${new_file}" 2>/dev/null | while IFS= read -r line || [[ -n "${line}" ]]; do
    case "${line}" in
      ---*|+++*|@@*)
        printf '%s\n' "${line}"
        ;;
      +*)
        pool_ledger_redact_line "${line#+}"
        ;;
      -*)
        pool_ledger_redact_line "${line#-}"
        ;;
      *)
        pool_ledger_redact_line "${line}"
        ;;
    esac
  done || true
}

pool_ledger_overwrite_env() {
  local example="$1"
  local dest="$2"
  local mode="$3"
  local yes_flag="${4:-0}"
  if [[ ! -f "${example}" ]]; then
    echo "ENV_ERROR missing example ${example}" >&2
    return 1
  fi
  if [[ -f "${dest}" && "${yes_flag}" -ne 1 ]]; then
    echo "ENV_REFUSE overwrite path=${dest} without --yes" >&2
    return 1
  fi
  local tmp backup_dir
  backup_dir="${POOL_LEDGER_DIR}/backups"
  if [[ -f "${dest}" ]]; then
    pool_ledger_backup_file "${dest}" "${backup_dir}"
  fi
  tmp="$(mktemp)"
  cp -a "${example}" "${tmp}"
  echo "--- redacted diff preview ---"
  pool_ledger_redacted_diff "${dest}" "${tmp}"
  install -m "${mode}" -o root -g azledger "${tmp}" "${dest}"
  rm -f "${tmp}"
  echo "ENV_OVERWRITE_OK path=${dest}"
}

pool_ledger_db_smoke_test() {
  if [[ ! -f "${COLLECTOR_ENV}" ]]; then
    echo "DB_SMOKE_SKIP missing ${COLLECTOR_ENV}"
    return 0
  fi
  if ! id azledger >/dev/null 2>&1; then
    echo "DB_SMOKE_SKIP user azledger missing"
    return 1
  fi
  if ! command -v psql >/dev/null 2>&1; then
    echo "DB_SMOKE_SKIP psql not installed"
    return 1
  fi
  sudo -u azledger -H bash --noprofile --norc -lc '
set -Eeuo pipefail
set -a
. '"${COLLECTOR_ENV}"'
set +a
DB="${DATABASE_URL:-${POSTGRES_LEDGER_DATABASE_URL:-${LEDGER_POSTGRES_DATABASE_URL:-}}}"
test -n "$DB"
psql "$DB" -v ON_ERROR_STOP=1 -Atc "SELECT 1;" | grep -qx 1
echo "DB smoke test: OK"
'
}

pool_ledger_report_execute_live_configured() {
  local path="${FRESH_CYCLE_ENV}"
  if [[ ! -f "${path}" ]]; then
    echo "EXECUTE_LIVE_CONFIG=unknown (missing fresh-cycle env)"
    return 0
  fi
  local mode enable phrase
  mode="$(grep -E '^AZCOIN_FRESH_CYCLE_AUTOMATION_MODE=' "${path}" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  enable="$(grep -E '^AZCOIN_FRESH_CYCLE_AUTOMATION_ENABLE_REAL_EXECUTION=' "${path}" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  phrase="$(grep -E '^AZCOIN_FRESH_CYCLE_AUTOMATION_RUNNER_APPROVAL_PHRASE=' "${path}" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  echo "FRESH_CYCLE_MODE=${mode:-unset}"
  if [[ -n "${enable}" ]]; then
    echo "FRESH_CYCLE_ENABLE_REAL_EXECUTION=<REDACTED>"
  else
    echo "FRESH_CYCLE_ENABLE_REAL_EXECUTION=unset"
  fi
  if [[ -n "${phrase}" ]]; then
    echo "FRESH_CYCLE_RUNNER_APPROVAL_PHRASE=<REDACTED>"
  else
    echo "FRESH_CYCLE_RUNNER_APPROVAL_PHRASE=unset"
  fi
  if [[ "${mode}" == "execute-live" || -n "${enable}" || -n "${phrase}" ]]; then
    echo "EXECUTE_LIVE_CONFIG=configured (report only; not changed by discovery)"
  else
    echo "EXECUTE_LIVE_CONFIG=not configured"
  fi
}

pool_ledger_systemd_environment_files() {
  local unit="$1"
  systemctl cat "${unit}" 2>/dev/null | awk '
    /^EnvironmentFile=/ {
      sub(/^EnvironmentFile=-?/, "")
      print $0
    }
  ' || true
}
