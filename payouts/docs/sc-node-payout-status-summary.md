# SC-node payout status summary (PR V)

Compact **read-only** view of a production execution and its **active** reconciliation state. Avoids running multiple admin commands and ad-hoc SQL during cycle closeout.

## Non-goals

- No wallet RPC, sends, or evidence export.
- No DB writes, migrations, or reconciliation matching changes.
- No automatic remediation.

## Prerequisites

- `DATABASE_URL` from collector env (read-only connection).
- Known `production_execution_id` for the cycle under review.

## Script

`payouts/scripts/sc_node_payout_status_summary.py`

```bash
cd /opt/azcoin-super/src/azpool
set -a
source /etc/azcoin-super/pool-ledger/collector.env
set +a
export PYTHONPATH=/opt/azcoin-super/src/azpool

.venv/bin/python payouts/scripts/sc_node_payout_status_summary.py \
  --production-execution-id 3
```

## Output fields

| Field | Meaning |
|-------|---------|
| `production_execution_id` | Execution under review |
| `payout_plan_id` | Linked payout plan |
| `execution_status` | e.g. `confirmed`, `partial_sent`, `sent` |
| `is_chunked_execution` | Chunked executor path when true |
| `chunk_summary` | Chunk counts, per-status counts, chunk rows (chunked only) |
| `active_reconciliation` | Active chunked reconciliation (`superseded_at IS NULL`) or single-send reconciliation by execution txid |
| `active_reconciliation.matched` | Whether reconciliation matched |
| `active_reconciliation.is_active` | Always true for single-send; false when superseded (chunked) |
| `expected_*` / `source_*` / `receiver_*` counts and totals | From reconciliation header |
| `supersedes_reconciliation_id` / `superseded_by_reconciliation_id` | Chunked supersede chain when present |

Evidence JSONB blobs are **omitted** from this summary (use admin `chunked-payout-reconciliation-details` with care if needed).

## Cycle #2-style closeout verification

For a closed chunked cycle (example: `production_execution_id=3`):

1. `execution_status` is `confirmed`.
2. `chunk_summary.chunk_status_counts` shows all chunks `confirmed`.
3. `active_reconciliation.kind` is `chunked`.
4. `active_reconciliation.matched` is `true`.
5. `active_reconciliation.is_active` is `true`.
6. `expected_chunk_count`, `source_chunk_count`, and `receiver_chunk_count` are equal.
7. `supersedes_reconciliation_id` / `superseded_by_reconciliation_id` reflect any supersede retry used during closeout.

If any check fails, **do not send coins** — investigate with reconciliation `preview` and a fresh receiver export ([sc-node-receiver-evidence-export.md](sc-node-receiver-evidence-export.md)).

## Manual validation checklist

- [ ] Command exits 0 for a known execution id.
- [ ] Wrong id returns exit 1 with clear stderr.
- [ ] Output is valid JSON.
- [ ] Values match admin `production-chunked-execution-details` and `chunked-payout-reconciliation-details` headers for the same ids.
