# SC-node current-state discovery and pool-ledger layout

Documents the **sc-2 protected pool-ledger layout** and installer safety rules for existing-node discovery vs fresh-node install.

## Protected pool-ledger layout (must preserve)

| Path | Owner:Group | Mode | Consumers |
|------|-------------|------|-----------|
| `/etc/azcoin-super/pool-ledger` | root:azledger | 0750 | all azledger services |
| `collector.env` | root:azledger | 0640 | collector, fresh-cycle, payout-scheduler |
| `fresh-cycle-automation.env` | root:azledger | 0640 | fresh-cycle automation |
| `payout-scheduler.env` | root:azledger | 0660 | payout scheduler (writable by azledger) |

When `azledger` cannot create temp files in the `0750` directory, fresh-cycle automation falls back to an in-place write of an **existing** `0660` scheduler env file (preserving inode ownership/mode). Atomic temp+replace is still preferred when the directory is writable.

### collector.env variables

- `DATABASE_URL` (required)
- `POOL_INSTANCES` (optional fallback)

### fresh-cycle-automation.env variables

- `AZCOIN_FRESH_CYCLE_AUTOMATION_MODE` (fresh default: `write-target`; never default to `execute-live`)
- `AZCOIN_FRESH_CYCLE_AUTOMATION_BASELINE`
- `AZCOIN_FRESH_CYCLE_AUTOMATION_WALLET`
- `AZCOIN_FRESH_CYCLE_AUTOMATION_AZC_BIN` (preview/preflight: `/usr/local/bin/azc-payout-readonly`)
- `AZCOIN_FRESH_CYCLE_AUTOMATION_AZC_BIN_EXECUTE` (execute-live only: `/usr/local/bin/azc-payout`)
- `AZCOIN_FRESH_CYCLE_AUTOMATION_RESERVE_FRACTION`
- `AZCOIN_FRESH_CYCLE_AUTOMATION_TARGET_SINGLE_TX_MAX_AMOUNT`
- `AZCOIN_FRESH_CYCLE_AUTOMATION_FALLBACK_CHUNK_AMOUNT`
- `AZCOIN_FRESH_CYCLE_AUTOMATION_ENABLE_REAL_EXECUTION` (never in fresh templates)
- `AZCOIN_FRESH_CYCLE_AUTOMATION_RUNNER_APPROVAL_PHRASE` (never in fresh templates)

### payout-scheduler.env variables

Fresh default:

```bash
SC_NODE_PAYOUT_SCHEDULER_MODE=report-only
```

## Secret source map

| Concern | Path |
|---------|------|
| AZCoin RPC auth | `/etc/azcoin/azcoin.conf` |
| Ledger DB | `/etc/azcoin-super/pool-ledger/collector.env` |
| Fresh-cycle policy | `/etc/azcoin-super/pool-ledger/fresh-cycle-automation.env` |
| Payout scheduler mode | `/etc/azcoin-super/pool-ledger/payout-scheduler.env` |
| WireGuard keys | `/etc/wireguard/keys/` |

Generated installer secrets under `/etc/azcoin-sc-node/secrets/` are for **future fresh installs only**. Do not rotate live sc-2 credentials automatically.

## Installer modes

### A) Existing-node discovery (default on configured hosts)

```bash
cd /opt/azcoin-super/src/azpool
sudo ./deploy/scripts/discover-sc-node-current-state.sh
sudo ./deploy/scripts/discover-sc-node-current-state.sh --write-report
```

- Detects systemd units and `EnvironmentFile=` paths
- Validates permissions (never prints `DATABASE_URL`)
- Reports execute-live posture **without changing it**
- Never shell-sources protected env files as a normal user (`benc`)
- systemd loads `EnvironmentFile=` as root before dropping to `User=azledger`

### B) Fresh-node install (explicit)

```bash
sudo ./deploy/scripts/install-sc-node-pool-ledger.sh
sudo ./deploy/scripts/install-sc-node-pool-ledger.sh --db-smoke-test   # after DATABASE_URL set
```

- Creates layout + missing env files from examples
- Safe defaults: report-only scheduler, no execute-live phrases
- Refuses overwrite unless `--yes` (timestamped backup + redacted diff, no secret printing)

Service unit installs (do not enable timers by default):

```bash
sudo ./deploy/scripts/install-azcoin-sc-node-payout-scheduler.sh
sudo ./deploy/scripts/install-azcoin-sc-node-fresh-cycle-automation.sh
```

## Validation: DATABASE_URL smoke test

Run as root; executes read-only `SELECT 1` as `azledger`. **Never prints `DATABASE_URL`.**

```bash
sudo -u azledger -H bash --noprofile --norc -lc '
set -Eeuo pipefail
set -a
. /etc/azcoin-super/pool-ledger/collector.env
set +a
DB="${DATABASE_URL:-${POSTGRES_LEDGER_DATABASE_URL:-${LEDGER_POSTGRES_DATABASE_URL:-}}}"
test -n "$DB"
psql "$DB" -v ON_ERROR_STOP=1 -Atc "SELECT 1;" | grep -qx 1
echo "DB smoke test: OK"
'
```

Or via discovery/install helpers:

```bash
sudo ./deploy/scripts/discover-sc-node-current-state.sh
sudo ./deploy/scripts/install-sc-node-pool-ledger.sh --db-smoke-test
```

## systemd env loading (do not shell-source as benc)

Units use:

```ini
EnvironmentFile=-/etc/azcoin-super/pool-ledger/collector.env
EnvironmentFile=-/etc/azcoin-super/pool-ledger/fresh-cycle-automation.env
```

Do **not** use `source /etc/azcoin-super/pool-ledger/*.env` in unit `ExecStart` as the dropped-privilege user. The directory is `root:azledger 0750`.

## Current-state exceptions (sc-2)

- `pool-sv2.service` runs as `benc` — document as test/dev exception; production target is a dedicated service user.
- `/etc/azcoin-sc-node/secrets/` exists but is not wired to live units until a deliberate fresh install.

## Related runbooks

- [pool-monitoring-collector.md](pool-monitoring-collector.md)
- [sc-node-fresh-cycle-automation.md](../../payouts/docs/sc-node-fresh-cycle-automation.md)
- [sc-node-payout-scheduler.md](../../payouts/docs/sc-node-payout-scheduler.md)
