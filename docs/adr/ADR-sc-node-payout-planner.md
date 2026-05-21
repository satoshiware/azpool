# ADR: SC-node payout plan generator (v0)

**Status:** Accepted (PR J — no-send proposals only)

**Date:** 2026-05-21

## Decision

The support node generates **draft payout plans** from draft SC-node credits (`sc_node_reward_credits`) and registered payout addresses (`sc_node_payout_addresses`). Plans are stored in `sc_node_payout_plans` and `sc_node_payout_plan_rows`.

**Payout plans are proposals only — not wallet transactions and not spend authorization.**

## Context

- PR I produces draft credits from mature reward events and mapped pool work.
- `support_wallet_reward_events` is gross history; **trusted wallet balance** is operator-supplied at plan time.
- Historical rewards may exceed current balance because funds were moved manually before automation.
- PR J enforces a **reserve fraction** (default 50%) against the operator's trusted balance snapshot.

## Scope (this PR)

| In scope | Out of scope |
|----------|----------------|
| Migration `007_sc_node_payout_plans.sql` | `sendtoaddress`, `sendmany`, `sendrawtransaction` |
| `preview` / `write-draft` CLI | `createrawtransaction`, `signrawtransaction*` |
| Reserve / max-spendable math | Wallet RPC / `azc` |
| One draft plan per `credit_run_id` (v0) | Payout execution or broadcast |
| Read-only admin `payout-plans` / `payout-plan-details` | Treating gross reward history as balance |

## Rules (v0)

- Credit run must be `status = draft` and `maturity_status = mature`.
- At least one draft credit required.
- Each credit requires exactly one `active` + `is_default` payout address.
- `planned_amount_total` must not exceed `max_spendable_amount`.
- `max_spendable_amount = trusted_balance_snapshot - reserve_amount`.
- `reserve_amount = trusted_balance_snapshot * reserve_fraction` (default 0.50).
- Refuse duplicate draft plan for same `credit_run_id`.

## Future PRs

Execution PRs must compare plans against **current trusted wallet balance** and operator reserve controls (`--reserve-amount`, `--max-spend-percent`) before any send.

## Consequences

- Operators can review proposed SC-node payouts without moving coins.
- Plans remain invalid as spend permission until a separate guarded execution PR approves and sends.
