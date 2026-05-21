# ADR: SC-node credit ledger (v0)

**Status:** Accepted (PR I — draft credits only, no payout execution)

**Date:** 2026-05-21

## Decision

The support node allocates **mature support-wallet reward events** to **SC nodes** (`sc_node_id`) using mapped pool work (`pool_share_work_deltas`) inside an operator-selected coverage window. PR I stores draft credit runs in Postgres only.

**PR I does not send coins, create payout transactions, call `azc`, inspect wallet balances, or generate payout plans.**

**SC-node credits are accounting records only — not spend authorization.** Draft credit rows must never be interpreted as permission to spend from the support wallet.

## Context

- PR H records gross reward-event history in `support_wallet_reward_events`.
- Historical mature rewards may exceed current wallet balance because funds were manually moved before the ledger became source-of-truth.
- `support_wallet_reward_events` is **not** spendable wallet balance — it is gross reward-event history.
- **Current trusted wallet balance** may be lower than summed mature reward events because the operator manually moved funds before automation.
- Pool telemetry uses interval columns `observed_from` / `observed_to`.
- Mature rewards may predate pool telemetry; blind allocation of all mature rows is forbidden.

## Scope (this PR)

| In scope | Out of scope |
|----------|----------------|
| Migration `006_sc_node_credit_ledger.sql` | Wallet RPC / `azc` |
| Preview + draft write CLI | Payout execution or broadcast |
| Read-only admin `credit-runs` / `credit-run-details` | Payout plan generator |
| Operator coverage cutover (`--coverage-start` / `--coverage-end`) | Wallet balance checks |
| Work-share allocation by mapped `sc_node_id` | `user_identity` payout splits |

## Eligibility rules

**Rewards (crediting input):**

- `support_wallet_reward_events.wallet_name` = selected wallet
- `maturity_status = 'mature'` only (`immature`, `orphaned`, etc. excluded)
- `event_time` within operator-selected `coverage_start` / `coverage_end`

**Work basis:**

- `pool_share_work_deltas` rows overlapping coverage via `observed_from` / `observed_to`
- Payable work: `sc_node_id IS NOT NULL`
- Unmapped work: `sc_node_id IS NULL` — reported, excluded from credits

**Refusal:**

- No eligible mature rewards in window
- `mapped_work_total = 0`
- Coverage gap / invalid window

## Coverage safeguards

| Mode | Coverage |
|------|----------|
| `preview` | Default = intersection(pool work range, mature reward range); operator may pass explicit `--coverage-start` / `--coverage-end` |
| `write-draft` | **Requires** explicit coverage bounds **or** `--allow-default-coverage` |

Operators must confirm selected mature rewards are still payable (not already manually moved/spent).

## Wallet balance and operator reserve (documentation only in PR I)

PR I has no wallet RPC and no `azc`, so it does **not** read or enforce wallet balance or spend caps.

Operators may reserve **at least 50%** of current trusted wallet balance for manual testing and future automated-transfer testing. That reserve policy is **not enforced** in PR I.

Future payout-plan and payout-execution PRs must support reserve controls, including:

- `--reserve-amount`
- `--max-spend-percent`
- A default max spend cap of **no more than 50%** of trusted wallet balance unless explicitly overridden

Those PRs must compare plans against **current trusted wallet balance** and operator reserve rules before any send.

## Credit run output fields

Each preview/write/print run exposes:

- `coverage_start`, `coverage_end`
- `reward_event_count`, `reward_amount_total`
- `mapped_work_total`, `unmapped_work_total`
- Per-SC-node `work_share`, `credit_amount` (draft)

## Future PRs

| PR | Scope |
|----|--------|
| **PR J** | Payout plan generator (no wallet sends); must respect balance + reserve controls |
| **PR K** | Guarded dry-run wallet payout execution |
| Later | Live execution: trusted balance check, reserve flags (`--reserve-amount`, `--max-spend-percent`), default ≤50% spend cap unless overridden |

## Consequences

- Draft SC-node credits exist without moving coins and **do not authorize spends**.
- Operators control cutover via explicit coverage windows.
- Reward-event totals must not be treated as spendable wallet balance.
- Payout planning/execution remains a separate approval path with balance and reserve enforcement.
