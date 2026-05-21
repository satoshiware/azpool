# SC-node production payout preflight (PR M)

Production-send **safety track** — evaluates approved payout plans against **current** wallet `getbalances`. **Does not send coins.**

## Prerequisites

- Migration `010_sc_node_payout_production_preflight.sql` applied.
- Payout plan `approved` with approved rows (PR K/J).
- Read-only wallet CLI available (e.g. `/tmp/azc` wrapper when `azc` is only a shell alias).

## Commands

Script: `payouts/scripts/sc_node_payout_production_preflight.py`

| Mode | Writes DB | Wallet RPC |
|------|-----------|------------|
| `preview` | No | `getbalances` only |
| `record` | Yes (preflight audit tables) | `getbalances` only |
| `details` | No | None |

## Examples

```bash
export DATABASE_URL='postgresql://...'

psql "$DATABASE_URL" -f payouts/migrations/010_sc_node_payout_production_preflight.sql

PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_production_preflight.py preview \
  --payout-plan-id 1 \
  --source-wallet-name wallet \
  --azc-bin /tmp/azc

PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_production_preflight.py record \
  --payout-plan-id 1 \
  --source-wallet-name wallet \
  --azc-bin /tmp/azc \
  --idempotency-key production-preflight-v0-plan-1

PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_production_preflight.py details \
  --production-preflight-id 1
```

### Reserve override (explicit)

```bash
PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_production_preflight.py preview \
  --payout-plan-id 1 \
  --source-wallet-name wallet \
  --azc-bin /tmp/azc \
  --override-reserve
```

Records `operator_override=true` on `record`. Still refuses `planned_amount_total > trusted_balance`.

## Read-only admin (no azc)

```bash
PYTHONPATH=. .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py production-preflights

PYTHONPATH=. .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py \
  production-preflight-details --production-preflight-id 1
```

## Safety notes

- **No sends** — only `getbalances` in this PR.
- **`support_wallet_reward_events` ≠ wallet balance** — always use current `getbalances` at execution time too (future PR).
- **Preflight audit ≠ spend authorization** — real sends are a later PR.
- **Do not mutate** `sc_node_payout_plans` / plan rows in PR M.

See [ADR-sc-node-production-payout-preflight.md](../../docs/adr/ADR-sc-node-production-payout-preflight.md).
