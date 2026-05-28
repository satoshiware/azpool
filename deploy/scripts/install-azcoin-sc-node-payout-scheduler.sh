#!/usr/bin/env bash
set -euo pipefail

# Install SC-node payout scheduler systemd units on the support node.
# Timer installation requires a non-empty OnCalendar schedule.
# Service-only install is safe: missing explicit target IDs => SAFE_SKIP exit 0.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=lib/pool-ledger-layout.sh
source "${REPO_ROOT}/deploy/scripts/lib/pool-ledger-layout.sh"
SERVICE_SRC="${REPO_ROOT}/deploy/systemd/azcoin-sc-node-payout-scheduler.service"
TIMER_TEMPLATE="${REPO_ROOT}/deploy/systemd/azcoin-sc-node-payout-scheduler.timer.template"
SERVICE_DST="/etc/systemd/system/azcoin-sc-node-payout-scheduler.service"
TIMER_DST="/etc/systemd/system/azcoin-sc-node-payout-scheduler.timer"
ENV_EXAMPLE="${REPO_ROOT}/deploy/systemd/payout-scheduler.env.example"
ENV_DST="${SCHEDULER_ENV}"

INSTALL_TIMER=0
ON_CALENDAR="${SC_NODE_PAYOUT_SCHEDULER_ON_CALENDAR:-}"

usage() {
  cat <<'EOF'
Usage: install-azcoin-sc-node-payout-scheduler.sh [--timer ON_CALENDAR]

Installs azcoin-sc-node-payout-scheduler.service (always).
Installs azcoin-sc-node-payout-scheduler.timer only when a non-empty schedule is provided.

Examples:
  sudo ./deploy/scripts/install-azcoin-sc-node-payout-scheduler.sh
  sudo SC_NODE_PAYOUT_SCHEDULER_ON_CALENDAR='Mon *-*-* 09:00:00' \
    ./deploy/scripts/install-azcoin-sc-node-payout-scheduler.sh --timer

Timer is NOT installed without an explicit OnCalendar value.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timer)
      INSTALL_TIMER=1
      shift
      if [[ $# -gt 0 && "$1" != --* ]]; then
        ON_CALENDAR="$1"
        shift
      fi
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

if [[ ! -f "${SERVICE_SRC}" ]]; then
  echo "ERROR: missing service unit: ${SERVICE_SRC}" >&2
  exit 1
fi

pool_ledger_ensure_layout

if [[ -f "${ENV_EXAMPLE}" && ! -f "${ENV_DST}" ]]; then
  install -m "${SCHEDULER_ENV_MODE}" -o root -g azledger "${ENV_EXAMPLE}" "${ENV_DST}"
  echo "ENV_EXAMPLE_OK path=${ENV_DST} (edit before enabling timer)"
elif [[ -f "${ENV_DST}" ]]; then
  chown root:azledger "${ENV_DST}"
  chmod "${SCHEDULER_ENV_MODE}" "${ENV_DST}"
  echo "ENV_PERMS_OK path=${ENV_DST} (root:azledger ${SCHEDULER_ENV_MODE})"
fi

install -m 0644 -o root -g root "${SERVICE_SRC}" "${SERVICE_DST}"
echo "SYSTEMD_OK service=${SERVICE_DST}"

if [[ "${INSTALL_TIMER}" -eq 1 ]]; then
  schedule="${ON_CALENDAR// /}"
  if [[ -z "${schedule}" ]]; then
    echo "ERROR: --timer requires non-empty OnCalendar schedule" >&2
    echo "Set SC_NODE_PAYOUT_SCHEDULER_ON_CALENDAR or pass schedule as argument." >&2
    echo "Timer was NOT installed. Service-only install is safe (SAFE_SKIP without target IDs)." >&2
    exit 1
  fi
  if [[ "${ON_CALENDAR}" == "@AZCOIN_PAYOUT_SCHEDULER_ON_CALENDAR@" ]]; then
    echo "ERROR: unresolved timer placeholder schedule" >&2
    exit 1
  fi
  if [[ ! -f "${TIMER_TEMPLATE}" ]]; then
    echo "ERROR: missing timer template: ${TIMER_TEMPLATE}" >&2
    exit 1
  fi
  sed "s|@AZCOIN_PAYOUT_SCHEDULER_ON_CALENDAR@|${ON_CALENDAR}|g" \
    "${TIMER_TEMPLATE}" > "${TIMER_DST}"
  echo "SYSTEMD_OK timer=${TIMER_DST} OnCalendar=${ON_CALENDAR}"
else
  if [[ -f "${TIMER_DST}" ]]; then
    echo "TIMER_SKIP existing timer preserved at ${TIMER_DST}"
  else
    echo "TIMER_SKIP no timer installed (pass --timer with non-empty OnCalendar to enable wake-ups)"
  fi
fi

systemctl daemon-reload
echo "DAEMON_RELOAD_OK"
