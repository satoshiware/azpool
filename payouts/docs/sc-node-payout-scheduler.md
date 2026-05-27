# SC-node payout scheduler v0 (PR Z)

Unattended **wrapper** around the PR Y manual-approved periodic payout runner. Default mode is **report-only** — no delegated execution, no wallet RPC, no fund movement.

## Prerequisites

- PR Y manual periodic payout runner installed in checkout.
- `DATABASE_URL` for read-only gate evaluation.
- Explicit IDs for v0 (no automatic plan/preflight discovery).

## Scheduler modes

| Mode | Default | Behavior |
|------|---------|----------|
| `report-only` | **Yes** | Evaluate cadence/readiness/preflight/idempotency gates; print report |
| `dry-run-delegate` | No | Subprocess PR Y runner with `--dry-run-delegate` |
| `execute-enabled` | No | Requires `--enable-real-execution YES_ENABLE_UNATTENDED_PAYOUT_EXECUTION` + env phrases; subprocess PR Y `execute-approved` |

## Required inputs (v0)

| Flag | Purpose |
|------|---------|
| `--payout-plan-id` | Approved plan |
| `--production-preflight-id` | Passed preflight row |
| `--recommended-execution-mode` | From PR X preflight preview (`single`/`chunked`/`halt`) |
| `--idempotency-key` | Required for delegate modes (or env) |

Optional: `--readiness-production-execution-id`, `--cycle-interval-minutes` (default 20 via env).

## Report-only example

```bash
export DATABASE_URL='postgresql://...'
export PYTHONPATH=/opt/azcoin-super/src/azpool

.venv/bin/python payouts/scripts/sc_node_payout_scheduler.py \
  --payout-plan-id 2 \
  --production-preflight-id 2 \
  --recommended-execution-mode chunked \
  --idempotency-key production-chunked-v0-plan-2
```

## Dry-run delegate example

```bash
export SC_NODE_PAYOUT_SCHEDULER_RUNNER_APPROVAL_PHRASE='YES_I_APPROVE_PERIODIC_SC_NODE_PAYOUT'
export SC_NODE_PAYOUT_SCHEDULER_EXECUTOR_CONFIRM_PHRASE='SEND CHUNKED 223.125000000000 FROM wallet FOR PLAN 2 IN 9 CHUNKS'
export SC_NODE_PAYOUT_SCHEDULER_SOURCE_WALLET_NAME='wallet'
export SC_NODE_PAYOUT_SCHEDULER_CHUNK_AMOUNT='25'

.venv/bin/python payouts/scripts/sc_node_payout_scheduler.py \
  --scheduler-mode dry-run-delegate \
  --payout-plan-id 2 \
  --production-preflight-id 2 \
  --recommended-execution-mode chunked \
  --idempotency-key production-chunked-v0-plan-2
```

## Execute-enabled (explicit opt-in only)

```bash
.venv/bin/python payouts/scripts/sc_node_payout_scheduler.py \
  --scheduler-mode execute-enabled \
  --enable-real-execution YES_ENABLE_UNATTENDED_PAYOUT_EXECUTION \
  ...
```

Refuses if approval phrases are not configured in env. **Not enabled by default.**

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success / eligible report / completed delegate |
| 2 | Safe skip (cadence not elapsed, gates closed) |
| 3 | HALT / NEEDS_EVIDENCE readiness |
| 1 | Usage / missing execute-enabled config |

## Optional systemd (disabled by default)

See `deploy/systemd/azcoin-sc-node-payout-scheduler.service` and `.timer`. **Do not enable** without separate operator decision. Timer unit defaults to `report-only` with placeholder plan/preflight IDs (`0`).

The shipped oneshot service template includes `SuccessExitStatus=2` so safe skip (exit 2) is not treated as failure by systemd. Exit 3 (HALT / unsafe) is **not** a success — investigate before retry. `execute-enabled` requires separate operator configuration (real IDs, approval phrases); never enable unattended real sends from the template alone.

## Safety

- No new wallet send primitives in scheduler code.
- All delegate paths go through PR Y runner → existing production/chunked executors.
- No bypass of cadence, readiness, preflight, or idempotency gates.

See [ADR-sc-node-payout-scheduler-v0.md](../../docs/adr/ADR-sc-node-payout-scheduler-v0.md).
