# ADR: SC-node payout UTXO/chunking preflight policy (v0)

**Status:** Accepted (PR X — read-only preflight extension, no sends)

**Date:** 2026-05-27

## Decision

Extend **production payout preflight** with read-only UTXO inspection (`listunspent`) and an operator-readable **UTXO/chunking policy** block. Preflight continues to **not send coins** and does not change execution behavior — it surfaces fragmentation risk and recommends `single`, `chunked`, or `halt` before an operator chooses an executor.

## Context

- Cycle #2 single-send (`production_execution_id=2`) failed with `-6 Transaction too large` due to wallet UTXO fragmentation.
- Chunked execution (`production_execution_id=3`) succeeded with 9 × 25 AZC chunks totaling 223.125 AZC.
- CEO guidance: payouts should be **periodic**, not immediate per block; configurable cadence comes later with a manual-approved runner.
- **25 AZC is not a permanent business max** — it remains a conservative **fallback chunk size** when fragmentation risk is elevated.
- The system should aim to support up to **500 AZC in one transaction** when UTXO/transaction-size policy says safe.

## Wallet interaction (read-only only)

| Allowed | Forbidden |
|---------|-----------|
| `getbalances` | `sendtoaddress`, `sendmany`, `sendrawtransaction` |
| `listunspent` (minconf=1) | `signrawtransaction`, `walletpassphrase`, `importprivkey`, `importmulti` |
| | `settxfee`, `bumpfee`, consolidation, raw tx construction |

Command shape:

```text
<azc-bin> -rpcwallet=<source-wallet-name> listunspent 1
```

If `listunspent` is unavailable or fails: `fragmentation_risk=UNKNOWN`, output states UTXO evidence is missing, and automation must **not** assume safe single-send.

Support-node wrapper: install `deploy/scripts/install-azc-payout-readonly-wrapper.sh` so `/usr/local/bin/azc-payout-readonly` allowlists `listunspent` alongside existing read-only RPCs.

## Policy outputs (preflight JSON `utxo_chunking_policy`)

- Balance/reserve fields: `spendable_balance`, `reserve_requirement`, `available_after_reserve`, `planned_payout_amount`
- UTXO evidence: `utxo_count`, `max_observed_utxo_amount`, `wallet_utxo_source`
- Limits: `target_single_tx_max_amount` (default 500 AZC), `fallback_chunk_amount` (default 25 AZC — **not** a protocol max)
- Recommendation: `recommended_chunk_size`, `estimated_chunk_count`, `fragmentation_risk`, `recommended_execution_mode`, `refusal_reason`

## Recommendation rules (v0)

1. **halt** when balance/reserve preflight fails (`execution_allowed=false`).
2. **chunked** when `planned_payout_amount > target_single_tx_max_amount` (500 AZC default).
3. **chunked** when fragmentation risk is `UNKNOWN`, `MEDIUM`, or `HIGH`.
4. **single** only when balance/reserve pass, planned amount ≤ 500 AZC, and fragmentation risk is `LOW`.

Fragmentation heuristics (v0): greedy input-count estimate from `listunspent` amounts; elevated UTXO count; never report `LOW` without evidence.

## Non-goals (this PR)

- Payout cadence / scheduler / unattended automation
- Wallet consolidation, `sendmany`, raw tx signing
- Changing production or chunked executor send behavior
- DB schema migrations for preflight audit columns (policy lives in preview/record JSON via existing notes path only if operator copies output — no new columns in v0)

## Periodic payout note

Preflight hardening supports **safer manual cycles** and future automation gates. It does **not** implement periodic payout scheduling — that remains a separate manual-approved runner change.
