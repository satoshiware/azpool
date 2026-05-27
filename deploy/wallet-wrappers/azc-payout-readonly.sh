#!/usr/bin/env bash
set -euo pipefail
exec sudo -n -u azcoin /usr/local/sbin/azc-payout-readonly-guard "$@"
