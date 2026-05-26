# ADR: SC-node chunked production payout executor (v0)

**Status:** Accepted (PR S)

**Date:** 2026-05-26

## Decision

Add a **chunked production executor** that pays an approved payout plan using multiple sequential `sendtoaddress` calls when a single send fails with UTXO fragmentation (e.g. `error code -6, Transaction too large`). Audit rows live in `sc_node_payout_production_execution_chunks`; execution #2 (refused, no txid) is not mutated.

## Context

- Plan #2: `223.125` AZC to one address; single-send execution #2 refused safely.
- Support wallet: 406 small UTXOs (~1–2 AZC); max UTXO 1.875 AZC.
- Chunk size `25` yields 9 sends (eight × 25 + one × 23.125).

## Wallet interaction

| Allowed | Forbidden |
|---------|-----------|
| `getbalances` (preview / pre-send) | `sendmany`, raw tx, sign, passphrase |
| `sendtoaddress` only in `execute-real`, sequential, stop on first failure | HTTP, bearer tokens, daemon/timer |

Confirmation phrase:

```text
SEND CHUNKED <planned_total> FROM <wallet> FOR PLAN <id> IN <chunk_count> CHUNKS
```

## Execution states

- All chunks sent → execution `sent`, chunks `sent`.
- Mid-sequence failure → execution `partial_sent`, failed chunk `refused`, later chunks remain `draft` (not sent).
- `mark-confirmed` only when execution `sent` and all chunks `sent`.

## Consequences

- Operators can complete plan #2 without `sendmany` or coin control merge.
- Reconciliation may need per-chunk txids in a future PR; v0 stores each chunk txid on chunk rows.
- Single-send executor unchanged.
