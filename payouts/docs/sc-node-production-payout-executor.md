# SC-node production payout executor (PR N)

First **real** support-wallet payout path. Uses `sendtoaddress` only in `execute-real`. **Irreversible on-chain** once sent.

## Prerequisites

- Migrations through `011_sc_node_payout_production_execution.sql`.
- Payout plan approved (PR K/J).
- Production preflight **passed** with `execution_allowed=true` (PR M).
- Explicit wallet CLI via `--azc-bin` (e.g. `/tmp/azc` when `azc` is a shell alias).

## Commands

Script: `payouts/scripts/sc_node_payout_production_executor.py`

| Mode | Sends coins | Wallet RPC |
|------|-------------|------------|
| `preview` (use first) | No | `getbalances` |
| `execute-real` | Yes | `getbalances` + `sendtoaddress` |
| `details` | No | None |
| `mark-confirmed` | No | None |

No daemon, timer, or background job.

## Examples

```bash
export DATABASE_URL='postgresql://...'

psql "$DATABASE_URL" -f payouts/migrations/011_sc_node_payout_production_execution.sql

# 1) Preview with fresh balance
PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_production_executor.py preview \
  --payout-plan-id 1 \
  --production-preflight-id 1 \
  --source-wallet-name wallet \
  --azc-bin /tmp/azc

# 2) Real send (only after inspecting preview output)
PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_production_executor.py execute-real \
  --payout-plan-id 1 \
  --production-preflight-id 1 \
  --source-wallet-name wallet \
  --azc-bin /tmp/azc \
  --idempotency-key production-real-v0-plan-1 \
  --confirm-phrase "SEND 121.875000000000 FROM wallet FOR PLAN 1"

# 3) Details / confirm
PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_production_executor.py details \
  --production-execution-id 1

PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_production_executor.py mark-confirmed \
  --production-execution-id 1
```

## Confirmation phrase

Exact format (amount uses 12 decimal places):

```text
SEND <planned_amount_total> FROM <source_wallet_name> FOR PLAN <payout_plan_id>
```

## Safety rules

- Re-checks **current** trusted balance at execution time (50% reserve default).
- Does **not** use `support_wallet_reward_events` as balance.
- Refuses address drift vs active/default registry.
- Refuses if active `sent`/`confirmed` execution exists for the plan (different idempotency key).
- v0 `execute-real` allows only one payout row unless `--allow-multiple-rows`.

## Read-only admin (no azc)

```bash
PYTHONPATH=. .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py production-executions

PYTHONPATH=. .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py \
  production-execution-details --production-execution-id 1
```

## Forbidden

- `sendmany`, raw tx RPCs, `walletpassphrase`, private key export.
- Automatic execution, systemd units, env-based wallet secrets in logs.
