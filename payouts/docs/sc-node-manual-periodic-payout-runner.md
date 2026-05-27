# SC-node manual-approved periodic payout runner (PR Y)

Manual operator coordination for **periodic** SC-node payouts. Evaluates cadence eligibility, optional readiness gate, production preflight state, and PR X `recommended_execution_mode` before delegating to existing single or chunked production executors.

**Not unattended automation** — no scheduler, daemon, timer, or background loop.

## Prerequisites

- Approved payout plan and passed production preflight (`record`).
- Fresh preflight `preview` JSON with `utxo_chunking_policy.recommended_execution_mode`.
- Existing production/chunked executor scripts and guarded wallet wrappers.
- `DATABASE_URL` for read-only gate queries.

## Cadence policy

| Setting | Default |
|---------|---------|
| `SC_NODE_PAYOUT_CYCLE_INTERVAL_MINUTES` | 20 |
| `--cycle-interval-minutes` | overrides env |
| `payout_cadence_policy` | `periodic` |
| `immediate_payout_allowed` | `false` |

Cadence anchor uses the latest **confirmed** production execution `updated_at` when available. If no reliable anchor exists, first cycle is eligible or operator supplies `--last-cycle-at`.

Manual early run requires **both**:

```bash
--override-cadence-check --override-cadence-reason "documented ops reason"
```

## Commands

Script: `payouts/scripts/sc_node_manual_periodic_payout_runner.py`

| Mode | Sends | Behavior |
|------|-------|----------|
| `preview` | No | Cadence + preflight + optional readiness/idempotency report |
| `execute-approved` | Via delegated executor only | Requires runner approval phrase + executor confirm phrase |

## Preview example

```bash
export DATABASE_URL='postgresql://...'
export PYTHONPATH=/opt/azcoin-super/src/azpool

.venv/bin/python payouts/scripts/sc_node_manual_periodic_payout_runner.py preview \
  --payout-plan-id 2 \
  --production-preflight-id 2 \
  --recommended-execution-mode chunked \
  --cycle-interval-minutes 20 \
  --idempotency-key production-chunked-v0-plan-2 \
  --readiness-production-execution-id 3
```

Preview JSON includes `gates.cadence`, `gates.idempotency`, and `gates.allowed`.

## Execute-approved example

```bash
.venv/bin/python payouts/scripts/sc_node_manual_periodic_payout_runner.py execute-approved \
  --payout-plan-id 2 \
  --production-preflight-id 2 \
  --recommended-execution-mode chunked \
  --cycle-interval-minutes 20 \
  --source-wallet-name wallet \
  --azc-bin /usr/local/bin/azc-payout \
  --idempotency-key production-chunked-v0-plan-2 \
  --chunk-amount 25 \
  --runner-approval-phrase YES_I_APPROVE_PERIODIC_SC_NODE_PAYOUT \
  --executor-confirm-phrase "SEND CHUNKED 223.125000000000 FROM wallet FOR PLAN 2 IN 9 CHUNKS"
```

Use `--dry-run-delegate` to print delegated executor argv without subprocess execution.

## Gate rules (execute-approved)

Refuses when any of:

- Cadence interval not elapsed (unless explicit override + reason)
- Runner approval phrase mismatch
- Preflight not `passed` / `execution_allowed=false`
- `recommended_execution_mode=halt`
- Readiness verdict `HALT` or `NEEDS_EVIDENCE` (when `--readiness-production-execution-id` supplied)
- Idempotency: existing `sent`/`confirmed` replay, `refused`/`partial_sent`, or other blocking plan execution

Delegates to:

- `sc_node_payout_production_executor.py execute-real` when mode=`single`
- `sc_node_payout_production_chunked_executor.py execute-real` when mode=`chunked`

## Safety

- Runner does **not** construct wallet send RPC argv directly.
- Executor confirmation phrases remain required (runner approval is additional).
- No wallet unlock, raw tx, sendmany, consolidation, or scheduler.

See [ADR-sc-node-manual-approved-periodic-payout-runner.md](../../docs/adr/ADR-sc-node-manual-approved-periodic-payout-runner.md).
