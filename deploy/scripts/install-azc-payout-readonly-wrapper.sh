#!/usr/bin/env bash
set -euo pipefail

# Install guarded read-only payout wallet wrappers on the support node.
# Allows: getbalances, gettransaction, listtransactions, listunspent (wallet only).
# Does not install send-capable azc-payout wrappers.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GUARD_SRC="${REPO_ROOT}/deploy/wallet-wrappers/azc-payout-readonly-guard.sh"
WRAPPER_SRC="${REPO_ROOT}/deploy/wallet-wrappers/azc-payout-readonly.sh"
GUARD_DST="/usr/local/sbin/azc-payout-readonly-guard"
WRAPPER_DST="/usr/local/bin/azc-payout-readonly"

if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: must run as root (use sudo)" >&2
  exit 1
fi

for src in "${GUARD_SRC}" "${WRAPPER_SRC}"; do
  if [[ ! -f "${src}" ]]; then
    echo "ERROR: missing source file: ${src}" >&2
    exit 1
  fi
done

install -m 0755 -o root -g root "${GUARD_SRC}" "${GUARD_DST}"
install -m 0755 -o root -g root "${WRAPPER_SRC}" "${WRAPPER_DST}"
echo "INSTALL_OK guard=${GUARD_DST} wrapper=${WRAPPER_DST}"
