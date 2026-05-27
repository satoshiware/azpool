# ADR: SC-node manual-approved periodic payout runner (v0)

**Status:** Accepted (PR Y — manual coordination only, no unattended automation)

**Date:** 2026-05-27

## Decision

Add a **manual-approved periodic payout runner** that coordinates existing payout tooling behind explicit operator gates:

1. Configurable **cadence eligibility** (default 20 minutes; env `SC_NODE_PAYOUT_CYCLE_INTERVAL_MINUTES`)
2. Optional **readiness** verdict check (PR W)
3. **Production preflight** passed state (DB)
4. PR X **`recommended_execution_mode`** (`single` / `chunked` / `halt`)
5. Exact runner approval phrase: `YES_I_APPROVE_PERIODIC_SC_NODE_PAYOUT`

Real sends delegate to existing production or chunked executor `execute-real` paths only.

## Context

- CEO guidance: payouts should be **periodic**, not immediate per-block.
- PR X surfaces UTXO/chunking recommendations before execution.
- PR W provides cycle closeout readiness verdicts.
- Existing executors already enforce confirmation phrases, reserve checks, and idempotency keys.

## Non-goals (this PR)

- Unattended scheduler / cron / systemd timer / daemon
- New wallet RPC send primitives in the runner
- sendmany, raw tx, wallet unlock, consolidation
- DB schema migrations for cadence tracking
- Changing reconciliation or executor internals

## Cadence (v0)

- Policy: `periodic`; `immediate_payout_allowed=false`
- Anchor: latest global `confirmed` production execution `updated_at` when present
- Conservative fallback: operator `--last-cycle-at` or `--override-cadence-check` + reason
- Minimum interval enforced as positive integer minutes (default 20)

## Idempotency (v0)

- Inspect plan executions before delegate
- Do not re-send when same idempotency key is already `sent`/`confirmed`
- Do not auto-retry `refused`, `partial_sent`, or blocking in-flight executions

## Approval model

Two layers for execute-approved:

1. Runner phrase: `YES_I_APPROVE_PERIODIC_SC_NODE_PAYOUT`
2. Existing executor phrase from preview (single or chunked)

Runner phrase is **additional**, not a replacement.

## Periodic payout note

This ADR enables **manual** periodic cycles today. Configurable cadence without override flags and unattended scheduling remain future work outside PR Y.
