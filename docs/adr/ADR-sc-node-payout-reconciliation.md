# ADR: SC-node payout post-execution reconciliation (v0)

**Status:** Accepted (PR O — reconciliation only, no sends)

**Date:** 2026-05-26

## Decision

Add a **read-only reconciliation** layer that runs **after** a confirmed production payout execution. It compares Postgres production execution audit rows, support-wallet `gettransaction` evidence, and optional receiving-side JSON exported manually from an SC-node wallet. PR O records audit rows only; it does **not** send coins, confirm executions, or mutate production execution status.

## Context

- PR N executed the first real SC-node production payout (`production_execution_id=1`, `txid` confirmed on ledger and receiver).
- Operators need an audit trail that cross-checks ledger, source wallet RPC, and receiver wallet export without bearer tokens or HTTP calls from this tool.
- `azc-payout` wrapper may not yet allow `gettransaction`; scripts accept explicit `--azc-bin` (e.g. `/usr/local/bin/azc-payout-readonly`).

## Wallet interaction (v0)

| Allowed | Forbidden |
|---------|-----------|
| Read-only `gettransaction` via subprocess (`shell=False`, explicit argv) | `sendtoaddress`, `sendmany`, `sendrawtransaction` |
| Optional read-only `getbalances` (not required in v0 script) | `createrawtransaction`, `signrawtransaction`, `walletpassphrase` |
| | Production execution, mark-confirmed, plan mutation |
| | HTTP requests, bearer tokens, SC-2 API calls from script |

Command shape:

```text
<azc-bin> -rpcwallet=<source-wallet-name> gettransaction <txid>
```

Receiver evidence: operator exports JSON (e.g. `/tmp/sc2-wallet-transactions.json`) and passes `--receiver-transactions-json`. No token handling in this tool.

## Reconciliation rules

- Only **confirmed** production executions are loaded for reconcile.
- Expected amount/address from production execution rows.
- Source `txid` must match execution `txid`; `confirmations >= 1` required for **matched**.
- Receiver (if provided): `txid`, `category=receive`, address and amount must match expected payout row.
- Receiver omitted: status **draft**, `matched=false`, `mismatch_reason='receiver evidence missing'`.
- Does **not** update `sc_node_payout_production_executions` or mark anything confirmed.

## Audit tables

- `sc_node_payout_reconciliations` — header with source/receiver snapshot fields and `reconciliation_status` (`draft` / `matched` / `mismatch` / `void`).
- `sc_node_payout_reconciliation_rows` — per production execution row comparison.

## Consequences

- Operators can document post-payout alignment across ledger, source wallet, and SC-node receive export.
- Reconciliation is not spend authorization and does not replace on-chain confirmation workflow in PR N.
- Read-only admin: `payout-reconciliations`, `payout-reconciliation-details` (no `azc`).
