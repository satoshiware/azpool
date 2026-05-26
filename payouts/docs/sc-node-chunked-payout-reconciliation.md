# SC-node chunked payout post-execution reconciliation (PR T)

Reconciliation runs **after** a **confirmed chunked** production payout execution. It compares:

1. Confirmed production execution chunk rows in Postgres (one txid per chunk)
2. Support/source wallet `gettransaction` evidence per chunk txid (read-only RPC)
3. Optional receiving-side evidence from a JSON file exported manually from an SC-node wallet/API

## Non-goals

- No sends (`sendtoaddress`, `sendmany`, `sendrawtransaction`, raw tx, sign, passphrase).
- No payout execution, chunked execute, or `mark-confirmed`.
- No mutation of production execution or chunk execution statuses.
- No HTTP requests, bearer tokens, or direct SC-2 API calls from this script.
- No daemon/timer.

## Migration

Apply `payouts/migrations/014_sc_node_chunked_payout_reconciliation.sql` (tables `sc_node_chunked_payout_reconciliations`, `sc_node_chunked_payout_reconciliation_chunks`).

## Script

`payouts/scripts/sc_node_chunked_payout_reconciliation.py` — requires `DATABASE_URL`.

### Preview (no DB writes)

Source-only (no receiver JSON):

```bash
PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_chunked_payout_reconciliation.py preview \
  --production-execution-id 3 \
  --source-wallet-name wallet \
  --azc-bin /usr/local/bin/azc-payout-readonly
```

With receiver JSON (required for `matched`):

```bash
PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_chunked_payout_reconciliation.py preview \
  --production-execution-id 3 \
  --source-wallet-name wallet \
  --azc-bin /usr/local/bin/azc-payout-readonly \
  --receiver-transactions-json /tmp/sc2-wallet-transactions.json
```

### Record (reconciliation audit tables only)

```bash
PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_chunked_payout_reconciliation.py record \
  --production-execution-id 3 \
  --source-wallet-name wallet \
  --azc-bin /usr/local/bin/azc-payout-readonly \
  --receiver-transactions-json /tmp/sc2-wallet-transactions.json
```

Record is **idempotent** on `production_execution_id` (unique). A repeat run with the same computed result returns the existing reconciliation (`recorded: false`, `idempotent_replay: true`). If an existing row disagrees with the newly computed preview, the command refuses without updating (`refusal_reason` in JSON, exit code 1).

### Details

```bash
PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_chunked_payout_reconciliation.py details \
  --reconciliation-id 1
```

Use `--include-raw-evidence` to return stored JSONB unchanged (default omits large `hex` from source evidence).

Wallet RPC (script only — one call per chunk txid):

```text
<azc-bin> -rpcwallet=<source-wallet-name> gettransaction <txid>
```

Receiver JSON may be a list of transaction objects or `{"transactions": [...]}`. Export from SC-2 wallet/API manually; this tool does not fetch it over HTTP.

## Read-only admin

```bash
PYTHONPATH=. .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py chunked-payout-reconciliations
PYTHONPATH=. .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py \
  chunked-payout-reconciliation-details --reconciliation-id 1
```

Admin commands are SELECT-only and do not call `azc`. Use `--include-raw-evidence` when debugging stored `gettransaction` payloads.

## Status semantics

| Status | Meaning |
|--------|---------|
| `source_only` | Source evidence valid; receiver JSON omitted |
| `matched` | All chunks align across ledger, source tx, and receiver evidence |
| `mismatch` | Missing receiver txid, amount/address/category mismatch, or source confirmations &lt; 1 |

`matched=true` only when receiver JSON is provided and every chunk passes receiver checks.

## Matching notes

- Source `gettransaction` amounts for sends are negative; matching uses absolute value.
- Expected total must equal sum of chunk amounts and `planned_amount_total` on the execution.
- Chunk count must match confirmed chunk rows on the execution.
