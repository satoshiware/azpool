#!/usr/bin/env bash
# Fresh-node pool-ledger install: safe defaults only; no execute-live secrets.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=lib/pool-ledger-layout.sh
source "${REPO_ROOT}/deploy/scripts/lib/pool-ledger-layout.sh"

COLLECTOR_EXAMPLE="${REPO_ROOT}/deploy/systemd/collector.env.example"
FRESH_CYCLE_EXAMPLE="${REPO_ROOT}/deploy/systemd/fresh-cycle-automation.env.example"
SCHEDULER_EXAMPLE="${REPO_ROOT}/deploy/systemd/payout-scheduler.env.example"

YES_OVERWRITE=0
RUN_DB_SMOKE=0

usage() {
  cat <<'EOF'
Usage: install-sc-node-pool-ledger.sh [--yes] [--db-smoke-test]

Fresh-node install mode:
- ensure /etc/azcoin-super/pool-ledger root:azledger 0750
- install missing env files from examples (safe report-only / write-target defaults)
- never include execute-live approval phrases in fresh templates
- refuse env overwrite unless --yes (with timestamped backup + redacted diff)
- optional --db-smoke-test after install (requires DATABASE_URL already set)

Existing-node: prefer discover-sc-node-current-state.sh (no overwrites).
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes)
      YES_OVERWRITE=1
      shift
      ;;
    --db-smoke-test)
      RUN_DB_SMOKE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: must run as root (use sudo)" >&2
  exit 1
fi

pool_ledger_ensure_layout
echo "LAYOUT_OK dir=${POOL_LEDGER_DIR} owner=${POOL_LEDGER_DIR_OWNER}:${POOL_LEDGER_DIR_GROUP} mode=${POOL_LEDGER_DIR_MODE}"

install_env() {
  local example="$1"
  local dest="$2"
  local mode="$3"
  if [[ -f "${dest}" ]]; then
    if [[ "${YES_OVERWRITE}" -eq 1 ]]; then
      pool_ledger_overwrite_env "${example}" "${dest}" "${mode}" 1
    else
      pool_ledger_install_env_if_missing "${example}" "${dest}" "${mode}"
    fi
  else
    pool_ledger_install_env_if_missing "${example}" "${dest}" "${mode}"
  fi
}

install_env "${COLLECTOR_EXAMPLE}" "${COLLECTOR_ENV}" "${COLLECTOR_ENV_MODE}"
install_env "${FRESH_CYCLE_EXAMPLE}" "${FRESH_CYCLE_ENV}" "${FRESH_CYCLE_ENV_MODE}"
install_env "${SCHEDULER_EXAMPLE}" "${SCHEDULER_ENV}" "${SCHEDULER_ENV_MODE}"

echo "SAFETY_NOTE services remain disabled until explicitly enabled; no execute-live secrets installed by default"

if [[ "${RUN_DB_SMOKE}" -eq 1 ]]; then
  pool_ledger_db_smoke_test
fi

echo "INSTALL_OK pool-ledger layout preserved"
