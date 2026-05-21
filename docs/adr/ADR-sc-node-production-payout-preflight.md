# ADR: SC-node production payout preflight (v0)

**Status:** Accepted (PR M — preflight only, no sends)

**Date:** 2026-05-21

## Decision

Add a **production payout preflight** layer that evaluates whether an approved payout plan could safely be executed against the **current** support-wallet trusted balance, with default **50% reserve** and optional explicit operator override. PR M records audit rows only; it does **not** send coins.

## Context

- PR K approves plans and records accounting preflight (`preflight_status`) against an operator-supplied trusted snapshot.
- PR L proves fake/regtest execution state transitions without wallet RPC.
- Live support wallet name is `wallet`; `azc` is a shell alias — Python must use an explicit binary such as `/tmp/azc` (wrapper to `azcoin-cli`).
- `support_wallet_reward_events` is gross reward history, **not** wallet balance.
- Credit ledger and payout plans are accounting records, **not** spend authorization.

## Wallet interaction (v0)

| Allowed | Forbidden |
|---------|-----------|
| Read-only `getbalances` via subprocess (`shell=False`, explicit argv) | `sendtoaddress`, `sendmany`, `sendrawtransaction` |
| | `createrawtransaction`, `signrawtransaction`, `walletpassphrase` |
| | Production execution, plan status mutation, real txids |

Command shape:

```text
<azc-bin> -rpcwallet=<source-wallet-name> getbalances
```

## Reserve and spend rules

- Default `reserve_percent` = 0.5 and `max_spend_percent` = 0.5.
- Without `--override-reserve`: refuse if `planned_amount_total > spendable_after_reserve` or above max spend percent cap.
- With `--override-reserve`: may exceed default reserve cap; `operator_override=true` on record.
- **Always** refuse if `planned_amount_total > trusted_balance` (even with override).
- Re-check active/default `sc_node_payout_addresses` for address drift.

## Audit tables

- `sc_node_payout_production_preflights` — header with balance snapshot, reserve math, `execution_allowed`, `preflight_status` (`passed` / `refused`).
- `sc_node_payout_production_preflight_rows` — per plan row check.

Preflight records are **not** execution authorization by themselves.

## Future production executor

A later PR may send coins only after fresh `getbalances`, reserve enforcement, and explicit spend authorization. PR M does not implement sends.

## Consequences

- Operators can rehearse production safety against live wallet balance without broadcasting.
- Default 50% reserve protects manual/automated testing funds on the support wallet.
- Admin visibility via read-only `production-preflights` commands (no `azc`).
