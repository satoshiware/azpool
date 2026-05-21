# ADR: SC-node payout plan approval and preflight (v0)

**Status:** Accepted (PR K — no-send review only)

**Date:** 2026-05-21

## Decision

Draft payout plans (`sc_node_payout_plans`) gain an operator **approval** workflow and a **no-send preflight** check before any future execution PR. PR K stores approval/preflight/cancellation metadata on the plan header.

**Approval and preflight are not spend authorization and do not move coins.**

## Context

- PR J creates no-send payout proposals from draft credits with reserve caps against an operator-supplied trusted balance snapshot.
- `support_wallet_reward_events` remains gross history, not wallet balance.
- Operators may reserve ≥50% of trusted balance for testing; execution PRs must enforce reserves at send time.

## Status transitions (v0)

| From | To | Action |
|------|-----|--------|
| `draft` | `approved` | `approve` with exact confirmation phrase |
| `draft` | `cancelled` | `cancel` |
| `approved` | `cancelled` | `cancel` |
| `approved` | (unchanged) | `preflight` records check only |

No execution statuses in PR K.

## Approval safeguards

- Confirmation phrase: `APPROVE PAYOUT PLAN <id> NO SEND`
- Plan must be `draft` with `row_count ≥ 1`, `planned_amount_total > 0`, within stored `max_spendable_amount`
- All rows `draft`; frozen payout addresses must match current active/default registry
- Stores `approval_confirmation_hash` (SHA-256 of phrase), not a reusable spend token

## Preflight safeguards

- Requires plan `approved`
- Operator supplies current `--trusted-balance-current`
- Recomputes reserve/max spendable; refuses if planned total exceeds current cap
- Re-checks address drift against registry
- Updates `preflight_checked_at`, `preflight_status` (`allowed` / `refused`), `preflight_note`
- **Does not** call `azc` or wallet RPC

## Future execution PRs

Must support `--reserve-amount`, `--max-spend-percent`, default ≤50% spend cap unless overridden, and compare plans to **current trusted wallet balance** before send.

## Consequences

- Operators can approve and preflight plans without broadcasting transactions.
- Approved plans remain proposals until a separate execution PR explicitly sends (out of scope here).
