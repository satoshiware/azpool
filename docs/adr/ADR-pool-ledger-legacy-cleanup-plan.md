# ADR: Pool ledger legacy cleanup plan (inventory only)

**Status:** Proposed (no deletions in this ADR or its introducing PR)

**Date:** 2026-05-19

## Decision

Before deleting any legacy user-level payout, settlement, or SQLite paths under `payouts/`, classify each file and subsystem as **ACTIVE**, **LEGACY-CANDIDATE**, **UNKNOWN**, or **DO-NOT-REMOVE-YET**. Removal happens only after explicit criteria are met and a replacement SC-node payout-credit design exists.

This ADR is an inventory and deprecation plan only. **No files are deleted here.**

## Context

The support-node architecture now targets:

- Credits/payouts to **SC node wallet addresses** only (`sc_node_id`).
- No support-node payment to individual pool worker `user_identity` values.
- Worker/customer revenue split **below** the SC node.
- `user_identity` as telemetry/audit input; payout math groups by `sc_node_id` only.
- Unmapped identities (`sc_node_id IS NULL`) remain unpaid.

Current delivered work is telemetry, registry, identity mapping, read-only reports, and admin visibility — not payout execution.

## Active current path (keep)

| Area | Role |
|------|------|
| `payouts/collector/app/*` | Pool monitoring collector, identity resolution, read-only SQL helpers |
| `payouts/collector/tests/*` | Collector and read-only unit tests |
| `payouts/scripts/sc_node_work_summary.py` | Read-only SC-node work summary JSON |
| `payouts/scripts/pool_ledger_admin_readonly.py` | Read-only pool-ledger admin JSON |
| `payouts/migrations/001_pool_telemetry_collector.sql` | Telemetry schema |
| `payouts/migrations/002_sc_node_identity_mapping.sql` | SC nodes + identity mappings |
| `payouts/migrations/003_pool_instance_registry.sql` | DB-backed pool registry |
| `docs/runbooks/pool-monitoring-collector.md` | Collector operations |
| `docs/runbooks/pool-ledger-admin.md` | Read-only admin operations |
| `deploy/systemd/azcoin-pool-collector.*` | Collector timer (outside this ADR’s edit scope) |

## Legacy-candidate areas (classify before removal)

| Path | Notes |
|------|------|
| `payouts/app/main.py` | Large FastAPI payout-service surface |
| `payouts/app/settlement.py` | User-level settlement logic |
| `payouts/app/postgres_settlement.py` | Postgres settlement path |
| `payouts/app/sender.py` | Payout send / wallet interaction |
| `payouts/app/postgres_sender.py` | Postgres-backed sender |
| `payouts/app/reward_contract.py` | Reward contract assumptions |
| `payouts/app/db.py` | SQLite and legacy DB helpers |
| `payouts/scripts/demo_interval_run.py` | Demo / interval payout runner |
| `payouts/scripts/backfill_postgres_shadow.py` | Shadow backfill |
| `payouts/scripts/backfill_sqlite_settlement_mapping.py` | SQLite settlement mapping backfill |
| `payouts/alembic/*` | Settlement / user payout schema history |
| `payouts/tests/*` (subset) | Tests asserting old user-level payout/settlement behavior |

## Do-not-remove-yet

- Anything still referenced by active **systemd** units, production imports, operator runbooks, or a **deployed API** service.
- **Historical migrations** until a deliberate DB migration/removal plan exists and is approved.
- Shared libraries used by both legacy payout API and new collector until import graph is untangled.
- Config templates and env examples (even if legacy paths read them).

## Removal criteria (all required)

1. No runtime or **systemd** references to the legacy module.
2. No active **imports** from production or collector code paths.
3. No **deployed API** dependency on the legacy endpoint or job.
4. Replacement **SC-node payout-credit** design implemented and reviewed.
5. Documented **rollback plan** (restore units, schema, or feature flags).
6. **Tests updated** — legacy tests removed or rewritten for SC-node model.

## Non-goals

- No file deletion in the PR that introduces this ADR.
- No wallet RPC additions.
- No payout execution or transaction broadcast.
- No schema migrations for cleanup.
- No telemetry backfill of historical `sc_node_id IS NULL` rows.

## Consequences

- Legacy code may remain duplicated until inventory is complete; operational risk is confusion, not immediate deletion.
- New work stays in `payouts/collector/` and read-only scripts until legacy paths are formally retired.

## Next PR recommendation (PR D)

Add a **legacy inventory artifact** (script or markdown matrix) mapping each file under `payouts/` to:

- **ACTIVE** — required for collector / read-only admin / current ops
- **LEGACY-CANDIDATE** — user-level payout/settlement; candidate for removal
- **UNKNOWN** — needs owner review
- **DO-NOT-REMOVE-YET** — still referenced or migration-critical

PR D should not delete files; it should produce the matrix and grep evidence (systemd, imports, docs links) to drive a later removal PR.
