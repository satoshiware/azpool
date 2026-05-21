# SC-node payout test/regtest executor (PR L)

Fake/regtest execution harness for approved payout plans. **Does not move real coins.**

## Prerequisites

- Migration `009_sc_node_payout_test_execution.sql` applied.
- Payout plan `approved` with `preflight_status=allowed` and rows `approved` (PR K).

## Commands

Script: `payouts/scripts/sc_node_payout_test_executor.py`

| Command | Writes DB | Notes |
|---------|-----------|-------|
| `preview` | No | Shows planned fake execution |
| `execute-fake` | Yes | Records fake txid + `sent` status |
| `mark-confirmed` | Yes | `sent` → `confirmed` |
| `details` | No | By `--test-execution-id` |

Required flags for fake execution:

- `--payout-plan-id`
- `--mode fake_regtest`
- `--test-wallet-name fake-regtest-wallet` (or other test-only name)
- `--idempotency-key` (for `execute-fake`)

## Examples

```bash
export DATABASE_URL='postgresql://...'

# Apply migration (once)
psql "$DATABASE_URL" -f payouts/migrations/009_sc_node_payout_test_execution.sql

# Preview (no writes) — safe on live approved plan 1
PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_test_executor.py preview \
  --payout-plan-id 1 \
  --mode fake_regtest \
  --test-wallet-name fake-regtest-wallet

# Fake execute (test tables only; does NOT touch production wallet)
PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_test_executor.py execute-fake \
  --payout-plan-id 1 \
  --mode fake_regtest \
  --test-wallet-name fake-regtest-wallet \
  --idempotency-key regtest-v0-run-1

# Confirm fake send
PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_test_executor.py mark-confirmed \
  --test-execution-id <ID_FROM_EXECUTE>

# Details
PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_test_executor.py details \
  --test-execution-id <ID>
```

## Read-only admin

```bash
PYTHONPATH=. .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py payout-test-executions

PYTHONPATH=. .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py \
  payout-test-execution-details --test-execution-id <ID>
```

## Safety

- Never use `wallet` or `support` as `--test-wallet-name`.
- Never expect real txids or on-chain confirmation from this harness.
- Production executor remains a future PR; see [ADR-sc-node-payout-test-executor.md](../../docs/adr/ADR-sc-node-payout-test-executor.md).
