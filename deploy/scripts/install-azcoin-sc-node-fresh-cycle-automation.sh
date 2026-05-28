#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE_SRC="${REPO_ROOT}/deploy/systemd/azcoin-sc-node-fresh-cycle-automation.service"
TIMER_TEMPLATE="${REPO_ROOT}/deploy/systemd/azcoin-sc-node-fresh-cycle-automation.timer.template"
SERVICE_DST="/etc/systemd/system/azcoin-sc-node-fresh-cycle-automation.service"
TIMER_DST="/etc/systemd/system/azcoin-sc-node-fresh-cycle-automation.timer"
ENV_EXAMPLE="${REPO_ROOT}/deploy/systemd/fresh-cycle-automation.env.example"
ENV_DST="/etc/azcoin-super/pool-ledger/fresh-cycle-automation.env"

INSTALL_TIMER=0
ON_CALENDAR="${AZCOIN_FRESH_CYCLE_AUTOMATION_ON_CALENDAR:-*:0/30}"

usage() {
  cat <<'EOF'
Usage: install-azcoin-sc-node-fresh-cycle-automation.sh [--timer ON_CALENDAR]

Installs azcoin-sc-node-fresh-cycle-automation.service (always).
Installs timer when --timer is passed or AZCOIN_FRESH_CYCLE_AUTOMATION_ON_CALENDAR is set.

Default timer schedule: *:0/30 (every 30 minutes).
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

install -d -o root -g root -m 0750 /etc/azcoin-super/pool-ledger
if [[ -f "${ENV_EXAMPLE}" && ! -f "${ENV_DST}" ]]; then
  install -m 0640 -o root -g azledger "${ENV_EXAMPLE}" "${ENV_DST}"
  echo "ENV_EXAMPLE_OK path=${ENV_DST}"
fi

install -m 0644 -o root -g root "${SERVICE_SRC}" "${SERVICE_DST}"
echo "SYSTEMD_OK service=${SERVICE_DST}"

if [[ "${INSTALL_TIMER}" -eq 1 ]]; then
  schedule="${ON_CALENDAR// /}"
  if [[ -z "${schedule}" ]]; then
    echo "ERROR: --timer requires non-empty OnCalendar schedule" >&2
    exit 1
  fi
  if [[ ! -f "${TIMER_TEMPLATE}" ]]; then
    echo "ERROR: missing timer template: ${TIMER_TEMPLATE}" >&2
    exit 1
  fi
  sed "s|@AZCOIN_FRESH_CYCLE_AUTOMATION_ON_CALENDAR@|${ON_CALENDAR}|g" \
    "${TIMER_TEMPLATE}" > "${TIMER_DST}"
  echo "SYSTEMD_OK timer=${TIMER_DST} OnCalendar=${ON_CALENDAR}"
fi

systemctl daemon-reload
echo "DAEMON_RELOAD_OK"
