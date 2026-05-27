# ADR: SC-node payout scheduler v0

**Status:** Accepted (PR Z — report-only default wrapper)

**Date:** 2026-05-27

## Decision

Add an **unattended scheduler wrapper** that reuses PR Y manual-approved periodic payout runner gates. Default mode is **report-only**. Optional `dry-run-delegate` and explicit `execute-enabled` modes delegate through PR Y — scheduler code introduces **no new wallet send primitives**.

## Context

- PR Y implements cadence eligibility, readiness/preflight enforcement, idempotency, and executor delegation.
- Automation runway requires a timer-friendly entrypoint without loosening custody boundaries.
- Production enablement must remain a deliberate operator decision.

## Modes

| Mode | Sends funds |
|------|-------------|
| `report-only` (default) | No |
| `dry-run-delegate` | No (PR Y `--dry-run-delegate`) |
| `execute-enabled` | Only via PR Y → existing executors when explicit flag + env phrases configured |

Real execution requires:

```text
--enable-real-execution YES_ENABLE_UNATTENDED_PAYOUT_EXECUTION
SC_NODE_PAYOUT_SCHEDULER_RUNNER_APPROVAL_PHRASE
SC_NODE_PAYOUT_SCHEDULER_EXECUTOR_CONFIRM_PHRASE
SC_NODE_PAYOUT_SCHEDULER_SOURCE_WALLET_NAME
SC_NODE_PAYOUT_SCHEDULER_IDEMPOTENCY_KEY
```

## v0 scope limits

- Explicit `--payout-plan-id`, `--production-preflight-id`, `--recommended-execution-mode` (no risky auto-discovery).
- Reuses PR Y cadence (`SC_NODE_PAYOUT_CYCLE_INTERVAL_MINUTES`, default 20).
- Optional systemd unit/timer shipped **disabled**; report-only command in service file.

## Non-goals

- New payout math, wallet RPC, sendmany, raw tx, wallet unlock, consolidation
- Bypassing PR Y gates or executor confirmation phrases
- Installing/enabling production timers in this PR

## Exit codes

- `0` success / eligible
- `2` safe skip (cadence/gates)
- `3` HALT / NEEDS_EVIDENCE
- `1` usage / missing execute config
