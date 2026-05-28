# SC-node fresh-cycle payout automation

Baseline-gated automation for **new** mature support-wallet rewards after Cycle #4. It never selects the historical unlinked backlog that existed before the automation baseline.

## What it automates

On each run the automation:

1. Computes `coverage_start = max(automation_baseline, latest credit_run.coverage_end)`
2. Selects **mature, unlinked** `wallet` reward events with `event_time >= coverage_start`
3. Uses half-open coverage `[coverage_start, coverage_end)` where `coverage_end` is just after the latest selected event
4. Previews or writes credit run → payout plan → approval → production preflight
5. Optionally delegates execution through the existing manual periodic payout runner (no new send primitives)

## Baseline

```text
AZCOIN_FRESH_CYCLE_AUTOMATION_BASELINE=2026-05-28T14:50:30+00:00
```

Cycle #4 (`credit_run_id=6`, `payout_plan_id=5`, `production_execution_id=8`) completed and confirmed at this baseline. Rewards before this timestamp are **historical backlog** — reported in preview JSON but never selected or credited by this automation.

## Modes

| Mode | Sends funds | Default |
|------|-------------|---------|
| `preview` | No | manual |
| `write-target` | No | **timer default** |
| `execute-live` | Only via existing runner gates | explicit env only |

## Environment

See `deploy/systemd/fresh-cycle-automation.env.example`.

Required for `execute-live`:

```text
AZCOIN_FRESH_CYCLE_AUTOMATION_ENABLE_REAL_EXECUTION=YES_ENABLE_FRESH_CYCLE_AUTOMATION
AZCOIN_FRESH_CYCLE_AUTOMATION_RUNNER_APPROVAL_PHRASE=YES_I_APPROVE_PERIODIC_SC_NODE_PAYOUT
```

## Commands

### Preview

```bash
cd /opt/azcoin-super/src/azpool
set -a && source /etc/azcoin-super/pool-ledger/collector.env && set +a
export PYTHONPATH=/opt/azcoin-super/src/azpool

.venv/bin/python payouts/scripts/sc_node_fresh_cycle_automation.py preview --json
```

Optional reward scan first:

```bash
.venv/bin/python payouts/scripts/sc_node_fresh_cycle_automation.py preview --scan-rewards-first --json
```

Zero fresh rewards → `SAFE_SKIP` exit 0.

Preview JSON includes wallet preflight fields when fresh rewards exist:

- `preflight_status`, `execution_allowed`, `trusted_balance`, `reserve_amount`, `spendable_after_reserve`
- `wallet_balance_source`, `utxo_chunking_policy`, `recommended_execution_mode`
- `refusal_reason` — always non-null when `recommended_execution_mode` is `halt`
- `azc_bin` — wallet CLI used for scan/preflight (defaults to `/usr/local/bin/azc-payout-readonly`, not bare `azc`)

Optional `AZCOIN_FRESH_CYCLE_AUTOMATION_MIN_PAYOUT_AMOUNT` refuses payouts below a configured threshold with an explicit refusal reason.

### Write-target (timer default)

```bash
.venv/bin/python payouts/scripts/sc_node_fresh_cycle_automation.py write-target --json
```

Writes credit/plan/approval/preflight and updates `/etc/azcoin-super/pool-ledger/payout-scheduler.env` with a **report-only** explicit target for operator review. Does not send funds.

### Execute-live (explicit only)

```bash
export AZCOIN_FRESH_CYCLE_AUTOMATION_MODE=execute-live
export AZCOIN_FRESH_CYCLE_AUTOMATION_ENABLE_REAL_EXECUTION=YES_ENABLE_FRESH_CYCLE_AUTOMATION
export AZCOIN_FRESH_CYCLE_AUTOMATION_RUNNER_APPROVAL_PHRASE=YES_I_APPROVE_PERIODIC_SC_NODE_PAYOUT

.venv/bin/python payouts/scripts/sc_node_fresh_cycle_automation.py execute-live --json
```

After execution the scheduler env is restored to safe report-only (no explicit target).

### Confirm sent executions

```bash
.venv/bin/python payouts/scripts/sc_node_fresh_cycle_automation.py confirm-sent --json
```

Finds `sent` FRESH-CYCLE production executions with non-null `txid` (ignores `refused` and txid-less rows). Single-tx executions delegate to production executor `mark-confirmed` with read-only `gettransaction` (`confirmations >= 1`) via `/usr/local/bin/azc-payout-readonly`. Chunked executions use the chunked executor confirm path. No sends.

### Wallet CLI selection

| Mode | Default binary | Env override |
|------|----------------|--------------|
| preview / write-target / confirm-sent | `/usr/local/bin/azc-payout-readonly` | `AZCOIN_FRESH_CYCLE_AUTOMATION_AZC_BIN` |
| execute-live (real send delegate) | `/usr/local/bin/azc-payout` | `AZCOIN_FRESH_CYCLE_AUTOMATION_AZC_BIN_EXECUTE` |

## Systemd install / enable

```bash
cd /opt/azcoin-super/src/azpool
sudo ./deploy/scripts/install-azcoin-sc-node-fresh-cycle-automation.sh --timer
sudo systemctl enable --now azcoin-sc-node-fresh-cycle-automation.timer
systemctl status azcoin-sc-node-fresh-cycle-automation.timer --no-pager
```

### Permissions model

- `/etc/azcoin-super/pool-ledger` is `root:azledger 0750` so the `azledger` service user can traverse the directory.
- `fresh-cycle-automation.env` is `root:azledger 0640` (read-only for service).
- `payout-scheduler.env` is `root:azledger 0660` so write-target/execute-live can update scheduler targets atomically.
- The unit uses systemd `EnvironmentFile=` (loaded as root before `User=azledger`); it does **not** shell-source env files.
- Re-run the install script after upgrades to normalize directory/file ownership if a prior install used `root:root 0750` on the pool-ledger directory.

Default timer schedule: `*:0/30` (every 30 minutes). Empty `OnCalendar` is rejected at install time.

Service runs `write-target` by default (from `fresh-cycle-automation.env`).

## Emergency disable

```bash
sudo systemctl disable --now azcoin-sc-node-fresh-cycle-automation.timer
sudo systemctl stop azcoin-sc-node-fresh-cycle-automation.service
```

Restore scheduler safe-skip if needed:

```bash
sudo install -m 0640 -o root -g azledger /dev/stdin \
  /etc/azcoin-super/pool-ledger/payout-scheduler.env <<'EOF'
SC_NODE_PAYOUT_SCHEDULER_MODE=report-only
EOF
```

## Rollback

```bash
sudo systemctl disable --now azcoin-sc-node-fresh-cycle-automation.timer
sudo rm -f /etc/systemd/system/azcoin-sc-node-fresh-cycle-automation.timer
sudo rm -f /etc/systemd/system/azcoin-sc-node-fresh-cycle-automation.service
sudo rm -f /etc/azcoin-super/pool-ledger/fresh-cycle-automation.env
sudo systemctl daemon-reload
```

## Safety

- No default coverage intersection — explicit baseline + latest credit run boundary only
- No historical backlog selection
- No new wallet send primitives in automation code
- execute-live delegates to existing manual periodic payout runner with cadence override reason `fresh-cycle-automation`
- Secrets/phrases redacted in log helper output
- write-target idempotency: reuses existing fresh-cycle credit run / plan / preflight for the same coverage window instead of duplicating rows; resumes payout plan write when a credit run exists without a plan
