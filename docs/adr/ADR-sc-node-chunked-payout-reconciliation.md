# ADR: SC-node chunked payout post-execution reconciliation (v0)

**Status:** Accepted (PR T — reconciliation only, no sends)

**Date:** 2026-05-26

## Decision

Add a **read-only chunked reconciliation** layer that runs **after** a confirmed **chunked** production payout execution (PR S). It compares Postgres chunk audit rows, per-chunk support-wallet `gettransaction` evidence, and optional receiving-side JSON exported manually from an SC-node wallet. PR T records audit rows only; it does **not** send coins, confirm executions, or mutate production execution or chunk statuses.

## Context

- Cycle #2 used the chunked production executor (`production_execution_id=3`, nine confirmed chunks, total 223.125 AZC).
- Single-tx reconciliation (PR O) does not model per-chunk txids; chunked payouts need per-txid source and receiver alignment.
- Operators need an audit trail that proves all chunk txids are present, confirmed on the source wallet, and received at the expected payout address without bearer tokens or HTTP from this tool.

## Wallet interaction (v0)

| Allowed | Forbidden |
|---------|-----------|
| Read-only `gettransaction` per chunk txid via subprocess (`shell=False`, explicit argv) | `sendtoaddress`, `sendmany`, `sendrawtransaction` |
| | `createrawtransaction`, `signrawtransaction`, `walletpassphrase`, key export |
| | Production execution, chunked execute, mark-confirmed, plan mutation |
| | HTTP requests, bearer tokens, SC-2 API calls from script |

Command shape (once per chunk):

```text
<azc-bin> -rpcwallet=<source-wallet-name> gettransaction <txid>
```

Receiver evidence: operator exports JSON (e.g. `/tmp/sc2-wallet-transactions.json`) and passes `--receiver-transactions-json`.

## Reconciliation rules

- Only **confirmed** production executions with **all** chunk rows `chunk_status=confirmed`.
- For each chunk: ledger txid and amount must match source `gettransaction` (absolute send amount) with `confirmations >= 1`.
- Receiver (if provided): match by txid, `category=receive`, address equals payout address, amount equals chunk amount.
- Receiver omitted: status **source_only**, `matched=false`.
- Receiver provided and all chunks pass: status **matched**, `matched=true`.
- Any missing receiver txid, amount/address mismatch, or unconfirmed source tx: **mismatch**.
- Does **not** update `sc_node_payout_production_executions` or chunk rows.

## Audit tables

- `sc_node_chunked_payout_reconciliations` — summary with totals, status, and JSONB evidence snapshots.
- `sc_node_chunked_payout_reconciliation_chunks` — per production execution chunk comparison.

Unique on `production_execution_id` for idempotent `record` on the **active** row (`superseded_at IS NULL`).

## Supersede / retry (PR U)

When an operator records reconciliation with stale receiver JSON, the active row may be `mismatch` while blocking a corrected record. PR U adds:

- `superseded_at`, `superseded_by_reconciliation_id`, `superseded_reason` on historical rows (evidence unchanged).
- Partial unique index: one active reconciliation per `production_execution_id`.
- Explicit `record --supersede-reconciliation-id` + `--supersede-reason` to supersede only a **non-matched** active row and insert a replacement in one transaction.
- Matched reconciliations cannot be superseded.

## Consequences

- Operators can document post-chunked-payout alignment across ledger, source wallet (N gettransaction calls), and SC-node receive export.
- Reconciliation is not spend authorization and does not replace on-chain confirmation workflow.
- Read-only admin: `chunked-payout-reconciliations`, `chunked-payout-reconciliation-details`.
- Admin output sanitizes nested `source_wallet_evidence` hex by default; `--include-raw-evidence` restores full stored JSONB.
- `record` replays when preview matches existing row; conflicting evidence is refused without DB updates.
