# SC-node payout scheduler v0 (PR Z)

Unattended **wrapper** around the PR Y manual-approved periodic payout runner. Default mode is **report-only** — no delegated execution, no wallet RPC, no fund movement.

**No blind auto-discovery:** the scheduler requires explicit approved target IDs (`payout_plan_id`, `production_preflight_id`, `recommended_execution_mode`). If any are missing, it logs `SAFE_SKIP` and exits **0**.

## Prerequisites

- PR Y manual periodic payout runner installed in checkout.
- `DATABASE_URL` for read-only gate evaluation (only when explicit target is configured).
- Explicit IDs for v0 (no automatic plan/preflight discovery).

## Scheduler modes

| Mode | Default | Behavior |
|------|---------|----------|
| `report-only` | **Yes** | Evaluate cadence/readiness/preflight/idempotency gates; print report |
| `dry-run-delegate` | No | Subprocess PR Y runner with `--dry-run-delegate` |
| `execute-enabled` | No | Requires `--enable-real-execution YES_ENABLE_UNATTENDED_PAYOUT_EXECUTION` + env phrases; subprocess PR Y `execute-approved` |

## Required inputs (v0)

Configure via CLI flags **or** env vars in `/etc/azcoin-super/pool-ledger/payout-scheduler.env` (see `deploy/systemd/payout-scheduler.env.example`):

| Flag / env | Purpose |
|------------|---------|
| `--payout-plan-id` / `SC_NODE_PAYOUT_SCHEDULER_PAYOUT_PLAN_ID` | Approved plan (positive integer) |
| `--production-preflight-id` / `SC_NODE_PAYOUT_SCHEDULER_PRODUCTION_PREFLIGHT_ID` | Passed preflight row |
| `--recommended-execution-mode` / `SC_NODE_PAYOUT_SCHEDULER_RECOMMENDED_EXECUTION_MODE` | From PR X preflight preview (`single`/`chunked`/`halt`) |
| `--scheduler-mode` / `SC_NODE_PAYOUT_SCHEDULER_MODE` | Default `report-only` |
| `--idempotency-key` / `SC_NODE_PAYOUT_SCHEDULER_IDEMPOTENCY_KEY` | Required for delegate modes |

Optional: `--readiness-production-execution-id`, `--cycle-interval-minutes` (default 20 via env).

## Report-only example

```bash
export DATABASE_URL='postgresql://...'
export PYTHONPATH=/opt/azcoin-super/src/azpool

.venv/bin/python payouts/scripts/sc_node_payout_scheduler.py \
  --payout-plan-id 2 \
  --production-preflight-id 2 \
  --recommended-execution-mode chunked \
  --idempotency-key production-chunked-v0-plan-2
```

## Dry-run delegate example

```bash
export SC_NODE_PAYOUT_SCHEDULER_RUNNER_APPROVAL_PHRASE='YES_I_APPROVE_PERIODIC_SC_NODE_PAYOUT'
export SC_NODE_PAYOUT_SCHEDULER_EXECUTOR_CONFIRM_PHRASE='SEND CHUNKED 223.125000000000 FROM wallet FOR PLAN 2 IN 9 CHUNKS'
export SC_NODE_PAYOUT_SCHEDULER_SOURCE_WALLET_NAME='wallet'
export SC_NODE_PAYOUT_SCHEDULER_CHUNK_AMOUNT='25'

.venv/bin/python payouts/scripts/sc_node_payout_scheduler.py \
  --scheduler-mode dry-run-delegate \
  --payout-plan-id 2 \
  --production-preflight-id 2 \
  --recommended-execution-mode chunked \
  --idempotency-key production-chunked-v0-plan-2
```

## Execute-enabled (explicit opt-in only)

```bash
.venv/bin/python payouts/scripts/sc_node_payout_scheduler.py \
  --scheduler-mode execute-enabled \
  --enable-real-execution YES_ENABLE_UNATTENDED_PAYOUT_EXECUTION \
  ...
```

Without `--enable-real-execution YES_ENABLE_UNATTENDED_PAYOUT_EXECUTION`, the scheduler logs `SAFE_SKIP` and exits **0** (no sends).

Refuses if approval phrases are not configured in env. **Not enabled by default.**

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success / eligible report / completed delegate / **SAFE_SKIP** (missing target or execute disabled) |
| 2 | Safe skip (cadence not elapsed, gates closed) |
| 3 | HALT / NEEDS_EVIDENCE readiness |
| 1 | Malformed target config |

## Systemd (disabled by default)

Units: `deploy/systemd/azcoin-sc-node-payout-scheduler.service` and `.timer.template`.

**Do not install the timer template directly** — it contains an `@AZCOIN_PAYOUT_SCHEDULER_ON_CALENDAR@` placeholder. Use the install script:

```bash
cd /opt/azcoin-super/src/azpool
sudo ./deploy/scripts/install-azcoin-sc-node-payout-scheduler.sh
```

Service-only install is safe: without target IDs in `payout-scheduler.env`, each run logs `SAFE_SKIP` and exits 0.

### Leave scheduler disabled (default)

```bash
# Install service only (no timer wake-ups)
sudo ./deploy/scripts/install-azcoin-sc-node-payout-scheduler.sh
# Do not pass --timer
```

### Configure one explicit approved target

```bash
sudo cp deploy/systemd/payout-scheduler.env.example \
  /etc/azcoin-super/pool-ledger/payout-scheduler.env
sudo chmod 640 /etc/azcoin-super/pool-ledger/payout-scheduler.env
sudo chown root:azledger /etc/azcoin-super/pool-ledger/payout-scheduler.env
# Edit: set SC_NODE_PAYOUT_SCHEDULER_PAYOUT_PLAN_ID, PRODUCTION_PREFLIGHT_ID,
# RECOMMENDED_EXECUTION_MODE. Keep SC_NODE_PAYOUT_SCHEDULER_MODE=report-only until reviewed.
```

### Verify safe-skip

```bash
sudo systemctl start azcoin-sc-node-payout-scheduler.service
journalctl -u azcoin-sc-node-payout-scheduler.service -n 20 --no-pager
# Expect: SAFE_SKIP: explicit payout target not configured ...
echo $?  # after manual run: 0
```

With target IDs set but `report-only`, expect gate evaluation output (still no sends).

### Enable timer

```bash
sudo SC_NODE_PAYOUT_SCHEDULER_ON_CALENDAR='Mon *-*-* 09:00:00' \
  ./deploy/scripts/install-azcoin-sc-node-payout-scheduler.sh --timer
sudo systemctl enable --now azcoin-sc-node-payout-scheduler.timer
systemctl status azcoin-sc-node-payout-scheduler.timer --no-pager
```

Empty or missing `OnCalendar` is rejected at install time — no invalid timer unit is written.

### Disable timer immediately

```bash
sudo systemctl disable --now azcoin-sc-node-payout-scheduler.timer
sudo rm -f /etc/systemd/system/azcoin-sc-node-payout-scheduler.timer
sudo systemctl daemon-reload
```

The oneshot service includes `SuccessExitStatus=2` so cadence/gate skips are not treated as failure. Exit 3 (HALT) is **not** success — investigate before retry.

## Safety

- No new wallet send primitives in scheduler code.
- No automatic discovery of payable cycles or plans.
- All delegate paths go through PR Y runner → existing production/chunked executors.
- No bypass of cadence, readiness, preflight, or idempotency gates.

See [ADR-sc-node-payout-scheduler-v0.md](../../docs/adr/ADR-sc-node-payout-scheduler-v0.md).
