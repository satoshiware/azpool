# SC-node production payout preflight (PR M + PR X)

Production-send **safety track** — evaluates approved payout plans against **current** wallet `getbalances` and read-only UTXO evidence from `listunspent`. **Does not send coins.**

## Prerequisites

- Migration `010_sc_node_payout_production_preflight.sql` applied.
- Payout plan `approved` with approved rows (PR K/J).
- Read-only wallet CLI available (e.g. `/usr/local/bin/azc-payout-readonly` on support node). Install/update from repo: `sudo deploy/scripts/install-azc-payout-readonly-wrapper.sh` (allows `getbalances`, `gettransaction`, `listtransactions`, `listunspent`).

## Commands

Script: `payouts/scripts/sc_node_payout_production_preflight.py`

| Mode | Writes DB | Wallet RPC |
|------|-----------|------------|
| `preview` | No | `getbalances` + `listunspent` |
| `record` | Yes (preflight audit tables) | `getbalances` + `listunspent` |
| `details` | No | None |

Use `--skip-utxo-inspection` only when `listunspent` is unavailable; UTXO policy will report `fragmentation_risk=UNKNOWN` and recommend chunked conservatively.

## Examples

```bash
export DATABASE_URL='postgresql://...'

PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_production_preflight.py preview \
  --payout-plan-id 1 \
  --source-wallet-name wallet \
  --azc-bin /usr/local/bin/azc-payout-readonly

PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_production_preflight.py record \
  --payout-plan-id 1 \
  --source-wallet-name wallet \
  --azc-bin /usr/local/bin/azc-payout-readonly \
  --idempotency-key production-preflight-v0-plan-1
```

### UTXO/chunking policy flags (PR X)

```bash
PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_production_preflight.py preview \
  --payout-plan-id 2 \
  --source-wallet-name wallet \
  --azc-bin /usr/local/bin/azc-payout-readonly \
  --target-single-tx-max-amount 500 \
  --fallback-chunk-amount 25
```

Preview JSON includes `utxo_chunking_policy`:

| Field | Meaning |
|-------|---------|
| `fragmentation_risk` | `LOW` / `MEDIUM` / `HIGH` / `UNKNOWN` |
| `recommended_execution_mode` | `single` / `chunked` / `halt` |
| `target_single_tx_max_amount` | Max single-send when policy says safe (default **500 AZC**) |
| `fallback_chunk_amount` | Conservative chunk size when fragmented (default **25 AZC** — not a business max) |
| `estimated_chunk_count` | Chunks if using chunked executor at `recommended_chunk_size` |
| `utxo_evidence_note` | Set when `listunspent` missing/failed |

**Cycle #2 lesson:** 223.125 AZC with fragmented UTXOs should show elevated fragmentation risk and recommend **chunked**, not single-send (explains execution #2 `Transaction too large`).

### Reserve override (explicit)

```bash
PYTHONPATH=. .venv/bin/python payouts/scripts/sc_node_payout_production_preflight.py preview \
  --payout-plan-id 1 \
  --source-wallet-name wallet \
  --azc-bin /usr/local/bin/azc-payout-readonly \
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

- **No sends** — only read-only `getbalances` and `listunspent`.
- **`execution_allowed`** remains balance/reserve/address policy only; UTXO policy is advisory via `recommended_execution_mode`.
- **25 AZC fallback ≠ max payout** — up to 500 AZC single-send is allowed when UTXO policy reports `LOW` risk.
- **Preflight audit ≠ spend authorization** — real sends use production or chunked executors separately.
- **Do not mutate** `sc_node_payout_plans` / plan rows.

See [ADR-sc-node-production-payout-preflight.md](../../docs/adr/ADR-sc-node-production-payout-preflight.md) and [ADR-sc-node-payout-utxo-chunking-preflight.md](../../docs/adr/ADR-sc-node-payout-utxo-chunking-preflight.md).
