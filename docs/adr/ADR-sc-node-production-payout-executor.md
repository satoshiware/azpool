# ADR: SC-node production payout executor (v0)

**Status:** Accepted (PR N — first real `sendtoaddress` path)

**Date:** 2026-05-21

## Decision

Add a **manual, operator-triggered** production payout executor that may call `sendtoaddress` only via the explicit CLI subcommand `execute-real`, after passed production preflight (PR M), fresh `getbalances`, 50% reserve enforcement, registry address validation, and an exact confirmation phrase.

## Context

- PR M records production preflight with `execution_allowed=true` but does not send coins.
- PR L proves fake/regtest execution without wallet RPC.
- Live support wallet: `wallet`; Python must use explicit `--azc-bin` (e.g. `/tmp/azc` wrapper).
- `support_wallet_reward_events` is **not** wallet balance.
- Plan 1 example: 121.875 AZC to sc-2 active/default address when all checks pass.

## Wallet interaction (v0)

| Allowed | Forbidden |
|---------|-----------|
| `getbalances` (preview + execute-real pre-check) | `sendmany`, `sendrawtransaction` |
| `sendtoaddress` (execute-real only, after checks) | `createrawtransaction`, `signrawtransaction` |
| subprocess `shell=False`, explicit argv | `walletpassphrase`, `dumpprivkey`, `createwallet`, `loadwallet` |

Command shapes:

```text
<azc-bin> -rpcwallet=<wallet> getbalances
<azc-bin> -rpcwallet=<wallet> sendtoaddress <address> <amount>
```

## Safety gates (execute-real)

1. Production preflight `passed` with `execution_allowed=true` for the plan/preflight ids.
2. Payout plan and rows `approved`; row count/amounts/addresses match preflight rows.
3. Fresh trusted balance via `getbalances` (do not trust preflight balance alone).
4. Default 50% reserve: refuse if `planned_amount_total > spendable_after_reserve`.
5. Refuse if `planned_amount_total > trusted_balance`.
6. Active/default registry address must match plan row (no drift).
7. Exact confirmation phrase: `SEND <amount> FROM <wallet> FOR PLAN <id>`.
8. Idempotency key; replay returns existing execution.
9. Refuse if another active (`sent`/`confirmed`) execution exists for the plan with a different key.
10. v0: refuse `execute-real` with multiple rows unless `--allow-multiple-rows`.

## Non-goals

- No systemd/timer/background daemon.
- No automatic execution; default CLI mode is `preview`.
- No payout plan status mutation.
- No `sendmany` or raw tx create/sign/broadcast.

## Audit tables

- `sc_node_payout_production_executions`
- `sc_node_payout_production_execution_rows`

Insert refused audit rows when checks fail before send. Insert `draft`, then mark `sent` after successful `sendtoaddress`.

## Consequences

- Operators can rehearse with `preview`, then send manually with irreversible on-chain effect.
- Admin read-only visibility: `production-executions`, `production-execution-details` (no `azc`).
