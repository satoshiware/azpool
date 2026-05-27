# SC-node payout cycle readiness gate (PR W)

Read-only **closeout / automation readiness** verdict for one production execution. Combines execution status, chunk progress, and active reconciliation state from Postgres only — **no wallet RPC**.

## Verdicts

| Verdict | Meaning | Exit code |
|---------|---------|-----------|
| `CLOSED` | Execution confirmed; active reconciliation matched; counts/totals align | 0 |
| `READY` | Safe to proceed to next operator step (v0: draft+passed preflight, or sent awaiting `mark-confirmed`) | 0 |
| `NEEDS_EVIDENCE` | Execution sent/confirmed but reconciliation or receiver/source evidence incomplete | 2 |
| `HALT` | Unsafe/ambiguous: refused/partial_sent, unmatched active reconciliation, multiple active rows, bad chunks, inconsistent supersede | 3 |

Usage/config errors (missing `DATABASE_URL`, unknown execution id) exit **1**.

v0 intentionally focuses on **CLOSED / NEEDS_EVIDENCE / HALT** for post-send cycles. `READY` is emitted only for unambiguous pre-closeout states documented below.

## Non-goals

- No sends, wallet RPC, DB writes, migrations, or reconciliation matching changes.
- No automatic payout execution.

## Script

`payouts/scripts/sc_node_payout_cycle_readiness.py`

### Operator text (default)

```bash
cd /opt/azcoin-super/src/azpool
set -a
source /etc/azcoin-super/pool-ledger/collector.env
set +a

.venv/bin/python payouts/scripts/sc_node_payout_cycle_readiness.py \
  --production-execution-id 3
```

No `PYTHONPATH` export is required; the script bootstraps the repo root from its own path.

### JSON (automation)

```bash
.venv/bin/python payouts/scripts/sc_node_payout_cycle_readiness.py \
  --production-execution-id 3 \
  --json
```

JSON includes: `verdict`, `exit_code`, execution/chunk/reconciliation fields, `missing_evidence_reasons`, `halt_reasons`, supersede linkage when present.

## Verdict rules (v0)

### CLOSED

- `execution_status = confirmed`
- Active reconciliation exists (`superseded_at IS NULL` for chunked)
- `matched = true`, `reconciliation_status = matched`
- Chunked: `expected_chunk_count = source_chunk_count = receiver_chunk_count =` execution chunk count; amount totals align
- Single-send: reconciliation `matched = true` for execution txid

### NEEDS_EVIDENCE

- `execution_status = confirmed` and no active reconciliation, **or**
- Active reconciliation missing receiver counts/amounts, **or**
- Chunked reconciliation `source_only`

### HALT

- `execution_status` in `refused`, `void`, `partial_sent`
- Multiple active chunked reconciliation rows for one execution
- Active reconciliation `matched = false`
- Active reconciliation with inconsistent supersede fields (`superseded_at` / `superseded_by_reconciliation_id` set while still “active”)
- Chunked execution: refused chunks, sent/confirmed chunks without txid, confirmed execution with non-confirmed chunks
- `matched = true` but closeout alignment checks fail (data inconsistency)

### READY

- `execution_status = draft` and linked preflight `preflight_status = passed` with `execution_allowed = true`
- `execution_status = sent` and all chunk rows `sent` with txids (chunked), **or** single-send with execution txid (awaiting `mark-confirmed`)

## Related tools

- Compact facts: [sc-node-payout-status-summary.md](sc-node-payout-status-summary.md)
- Receiver export: [sc-node-receiver-evidence-export.md](sc-node-receiver-evidence-export.md)
- Cycle checklist: [sc-node-payout-cycle.md](../../docs/runbooks/sc-node-payout-cycle.md)

## Manual validation checklist

- [ ] Closed cycle #2 execution returns `CLOSED` / exit 0
- [ ] Confirmed execution without reconciliation returns `NEEDS_EVIDENCE` / exit 2
- [ ] Known mismatch reconciliation returns `HALT` / exit 3
- [ ] Text output readable; `--json` parses and includes `verdict` + `exit_code`
