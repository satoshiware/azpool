# SC-node chunked production payout executor (PR S)

Pays an approved production plan using **multiple `sendtoaddress` calls** when UTXO fragmentation blocks a single send.

Script: `payouts/scripts/sc_node_payout_production_chunked_executor.py`

## Migration

```bash
psql "$DATABASE_URL" -f payouts/migrations/013_sc_node_payout_production_execution_chunks.sql
```

Adds `sc_node_payout_production_execution_chunks` and execution status `partial_sent`.

## Commands

| Command | Sends | Wallet RPC |
|---------|-------|------------|
| `preview` | No | `getbalances` |
| `execute-real` | Yes (sequential) | `getbalances` + `sendtoaddress` |
| `mark-confirmed` | No | None |
| `details` | No | None |

## Plan #2 example (`chunk-amount` 25)

```bash
export PYTHONPATH=/opt/azcoin-super/src/azpool
# preview
.venv/bin/python payouts/scripts/sc_node_payout_production_chunked_executor.py preview \
  --payout-plan-id 2 \
  --production-preflight-id 2 \
  --source-wallet-name wallet \
  --chunk-amount 25 \
  --azc-bin /usr/local/bin/azc-payout-readonly

# execute (after inspecting preview; exact phrase from preview output)
.venv/bin/python payouts/scripts/sc_node_payout_production_chunked_executor.py execute-real \
  --payout-plan-id 2 \
  --production-preflight-id 2 \
  --source-wallet-name wallet \
  --chunk-amount 25 \
  --azc-bin /usr/local/bin/azc-payout \
  --idempotency-key production-chunked-v0-plan-2 \
  --confirm-phrase "SEND CHUNKED 223.125000000000 FROM wallet FOR PLAN 2 IN 9 CHUNKS"

# details
.venv/bin/python payouts/scripts/sc_node_payout_production_chunked_executor.py details \
  --production-execution-id <NEW_EXECUTION_ID>

# after confirmations
.venv/bin/python payouts/scripts/sc_node_payout_production_chunked_executor.py mark-confirmed \
  --production-execution-id <NEW_EXECUTION_ID>
```

## Admin (read-only)

```bash
.venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py \
  production-chunked-execution-details --production-execution-id <ID>
```

## Safety

- Refuses if another `sent` / `confirmed` / `partial_sent` execution exists for the plan (different idempotency key).
- Allows retry when prior execution is `refused` (e.g. execution #2).
- Idempotent replay on same `(payout_plan_id, idempotency_key)`.
- Stops sending after first chunk failure; does not mark confirmed in `execute-real`.

See [ADR-sc-node-production-payout-chunked-executor.md](../../docs/adr/ADR-sc-node-production-payout-chunked-executor.md).
