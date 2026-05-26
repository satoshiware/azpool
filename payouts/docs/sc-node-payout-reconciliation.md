# SC-node payout post-execution reconciliation (PR O)

Reconciliation runs **after** a confirmed production payout execution. It compares:

1. Production execution audit rows in Postgres
2. Support/source wallet `gettransaction` evidence (read-only RPC)
3. Optional receiving-side evidence from a JSON file exported manually from an SC-node wallet/API

## Non-goals

- No sends (`sendtoaddress`, `sendmany`, `sendrawtransaction`, raw tx, sign, passphrase).
- No payout execution or `mark-confirmed`.
- No mutation of production execution statuses.
- No HTTP requests, bearer tokens, or direct SC-2 API calls from this script.
- No daemon/timer.

## Migration

Apply `payouts/migrations/012_sc_node_payout_reconciliation.sql` (tables `sc_node_payout_reconciliations`, `sc_node_payout_reconciliation_rows`).

## Script

`payouts/scripts/sc_node_payout_reconciliation.py` — requires `DATABASE_URL`.

### Preview (no DB writes)

```bash
PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_reconciliation.py preview \
  --production-execution-id 1 \
  --source-wallet-name wallet \
  --azc-bin /usr/local/bin/azc-payout-readonly \
  --receiver-transactions-json /tmp/sc2-wallet-transactions.json
```

### Record (reconciliation audit tables only)

```bash
PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_reconciliation.py record \
  --production-execution-id 1 \
  --source-wallet-name wallet \
  --azc-bin /usr/local/bin/azc-payout-readonly \
  --receiver-transactions-json /tmp/sc2-wallet-transactions.json
```

### Details

```bash
PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_reconciliation.py details \
  --reconciliation-id 1
```

Wallet RPC (script only):

```text
<azc-bin> -rpcwallet=<source-wallet-name> gettransaction <txid>
```

Receiver JSON may be a list of transaction objects or `{"transactions": [...]}`. Export from SC-2 wallet/API manually; this tool does not fetch it over HTTP.

## Read-only admin

```bash
PYTHONPATH=. .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py payout-reconciliations
PYTHONPATH=. .venv/bin/python payouts/scripts/pool_ledger_admin_readonly.py payout-reconciliation-details --reconciliation-id 1
```

Admin commands are SELECT-only and do not call `azc`.

## Status semantics

| Status | Meaning |
|--------|---------|
| `draft` | Receiver evidence missing (partial reconcile) |
| `matched` | Ledger, source tx, and receiver evidence align |
| `mismatch` | Txid, amount, address, category, or source confirmations failed |
| `void` | Reserved for operator voiding (not set by v0 script) |

See [ADR-sc-node-payout-reconciliation.md](../../docs/adr/ADR-sc-node-payout-reconciliation.md).
