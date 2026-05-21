# SC-node credit ledger (v0)

Allocate **mature** support-wallet reward events to **SC nodes** by mapped pool work inside an operator-selected coverage window. **Draft credits only** — no sends, no `azc`, no payout plans.

See also: [ADR-sc-node-credit-ledger.md](../adr/ADR-sc-node-credit-ledger.md), [support-wallet-reward-listener.md](support-wallet-reward-listener.md), [pool-ledger-admin.md](pool-ledger-admin.md)

## Critical accounting warning

**SC-node credits are accounting records only — not spend authorization.** A draft `sc_node_reward_credit_runs` row or per-node `credit_amount` must **never** be read as permission to spend from the support wallet.

`support_wallet_reward_events` is **gross reward-event history**, not spendable wallet balance.

The support wallet may contain **historical mature rewards** while **current trusted wallet balance is lower** because the operator manually moved funds before the ledger became source-of-truth.

**Do not credit all mature reward events blindly.**

Before `write-draft`, confirm the selected `coverage_start` / `coverage_end` window contains only rewards you intend to treat as still payable. PR I does not call `azc` and does not inspect wallet balances.

## Operator reserve (not enforced in PR I)

Operators may keep **at least 50%** of current trusted wallet balance reserved for manual testing and future automated-transfer testing.

PR I does **not** enforce that reserve (no wallet RPC, no `azc`). It is documented policy only until payout planning/execution exists.

Future payout-plan and payout-execution PRs must support reserve controls such as:

- `--reserve-amount`
- `--max-spend-percent`
- Default max spend cap: **no more than 50%** of trusted wallet balance unless explicitly overridden

Those PRs must compare plans against **current trusted wallet balance** and operator reserve rules before sending.

## What PR I does / does not do

| Does | Does not |
|------|----------|
| Preview proportional SC-node draft credits | Send coins |
| Insert draft `sc_node_reward_credit_runs` | Create/sign/broadcast transactions |
| Link included mature reward events | Call `azc` or wallet RPC |
| Report unmapped work (excluded from credits) | Generate payout plans |
| Require explicit coverage for writes (unless `--allow-default-coverage`) | Treat reward-event totals as wallet balance |
| Produce draft accounting credits | Authorize spends or payout execution |

## Apply migration

```bash
cd /opt/azcoin-super/src/azpool/payouts
set -a
source /etc/azcoin-super/pool-ledger/collector.env
set +a
psql "$DATABASE_URL" -f migrations/006_sc_node_credit_ledger.sql
psql "$DATABASE_URL" -c "\d sc_node_reward_credit_runs"
```

## Preview (JSON only)

Default coverage = intersection(pool telemetry `observed_from`/`observed_to` range, mature reward `event_time` range):

```bash
cd /opt/azcoin-super/src/azpool
set -a
source /etc/azcoin-super/pool-ledger/collector.env
set +a
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/sc_node_credit_ledger.py preview --wallet SUPPORT
```

Operator-selected cutover window:

```bash
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/sc_node_credit_ledger.py preview \
  --wallet SUPPORT \
  --coverage-start 2026-05-01T00:00:00+00:00 \
  --coverage-end 2026-05-20T23:59:59+00:00
```

Preview output includes: `coverage_start`, `coverage_end`, `reward_event_count`, `reward_amount_total`, `mapped_work_total`, `unmapped_work_total`, and per-SC-node draft credits. No `user_identity` in default output.

## Write draft (requires explicit coverage)

```bash
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/sc_node_credit_ledger.py write-draft \
  --wallet SUPPORT \
  --run-label manual-2026-05-21 \
  --coverage-start 2026-05-01T00:00:00+00:00 \
  --coverage-end 2026-05-20T23:59:59+00:00
```

Refuses when:

- Missing explicit coverage and no `--allow-default-coverage`
- No eligible mature rewards in window
- `mapped_work_total` is zero

Optional default intersection (use only after operator review):

```bash
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/sc_node_credit_ledger.py write-draft \
  --wallet SUPPORT \
  --run-label manual-default-coverage \
  --allow-default-coverage
```

## Print stored runs

```bash
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/sc_node_credit_ledger.py print

PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/sc_node_credit_ledger.py print --credit-run-id 1
```

## Read-only admin

```bash
PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/pool_ledger_admin_readonly.py credit-runs

PYTHONPATH=/opt/azcoin-super/src/azpool .venv/bin/python \
  payouts/scripts/pool_ledger_admin_readonly.py credit-run-details --credit-run-id 1
```

Admin commands are DB read-only (no `azc`, no raw wallet events).

## Eligibility summary

- **Rewards:** `maturity_status = 'mature'` only; `immature` / `orphaned` excluded
- **Work:** interval overlap on `observed_from` / `observed_to`; mapped `sc_node_id` only for payable share
- **Unmapped:** `sc_node_id IS NULL` reported as `unmapped_work_total`, not credited

## Next PR

**PR J:** Payout plan generator (still no wallet sends in that phase).
