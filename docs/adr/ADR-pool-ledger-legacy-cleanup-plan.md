# ADR: Pool ledger legacy cleanup plan

**Status:** Accepted (inventory PR D — no code deletion)

**Date:** 2026-05-19

## Decision

**Maintain** the current collector, read-only reporting, and read-only admin paths as **ACTIVE**. **Classify** old user-level payout, settlement, SQLite, and FastAPI service code as **LEGACY-CANDIDATE**. **Require** file-level inventory verification (see [payout-ledger-file-inventory.md](../inventory/payout-ledger-file-inventory.md)) before any deletion or quarantine.

**PR D (`feature/payout-ledger-legacy-inventory-v0`) does not delete, move, or rename code.** It only adds documentation, a read-only inventory script, and tests.

**Future payouts** will be **SC-node-level** (`sc_node_id` → SC node wallet), not worker/user-level (`user_identity`). The support node does not pay individual pool workers; splits happen below the SC node.

**Historical telemetry** with `sc_node_id IS NULL` remains **unpaid** unless a separate, explicitly approved backfill is executed. Inventory and cleanup PRs do not perform backfill.

## Context

The support-node architecture now targets:

- Credits/payouts to **SC node wallet addresses** only (`sc_node_id`).
- No support-node payment to individual pool worker `user_identity` values.
- Worker/customer revenue split **below** the SC node.
- `user_identity` as telemetry/audit input; payout math groups by `sc_node_id` only.
- Unmapped identities (`sc_node_id IS NULL`) remain unpaid.

Current delivered work is telemetry, registry, identity mapping, read-only reports, and admin visibility — not payout execution.

The repo retains a large **legacy-candidate** tree from an earlier worker/user payout and settlement design. That code must not be deleted without the inventory matrix, verification commands, and removal criteria below.

## Inventory artifact (PR D)

| Artifact | Purpose |
|----------|---------|
| [docs/inventory/payout-ledger-file-inventory.md](../inventory/payout-ledger-file-inventory.md) | Human-readable matrix: classification, evidence, proposed action, risk |
| `payouts/scripts/inventory_payout_ledger_files.py` | Read-only JSON scan (paths, keywords, suggested classification) |
| `payouts/collector/tests/test_payout_ledger_inventory.py` | Unit tests for classification rules and script output shape |

Classifications are only: **ACTIVE**, **LEGACY-CANDIDATE**, **UNKNOWN**, **DO-NOT-REMOVE-YET**.

## Active current path (keep)

| Area | Role |
|------|------|
| `payouts/collector/app/*` | Pool monitoring collector, identity resolution, read-only SQL helpers |
| `payouts/collector/tests/*` | Collector and read-only unit tests |
| `payouts/scripts/sc_node_work_summary.py` | Read-only SC-node work summary JSON |
| `payouts/scripts/pool_ledger_admin_readonly.py` | Read-only pool-ledger admin JSON |
| `payouts/scripts/inventory_payout_ledger_files.py` | PR D inventory helper (read-only) |
| `payouts/migrations/001_pool_telemetry_collector.sql` | Telemetry schema |
| `payouts/migrations/002_sc_node_identity_mapping.sql` | SC nodes + identity mappings |
| `payouts/migrations/003_pool_instance_registry.sql` | DB-backed pool registry |
| `docs/runbooks/pool-monitoring-collector.md` | Collector operations |
| `docs/runbooks/pool-ledger-admin.md` | Read-only admin operations |
| `deploy/systemd/azcoin-pool-collector.*` | Collector timer (outside PR D edit scope) |

## Legacy-candidate areas (classify before removal)

See the full matrix in [payout-ledger-file-inventory.md](../inventory/payout-ledger-file-inventory.md). Summary:

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
| `payouts/tests/*` (subset) | Tests asserting old user-level payout/settlement behavior |

## Do-not-remove-yet

- Anything still referenced by active **systemd** units, production imports, operator runbooks, or a **deployed API** service.
- **Historical Alembic** revisions under `payouts/alembic/versions/` until a deliberate DB migration/removal plan exists and is approved.
- Applied **telemetry migrations** (`payouts/migrations/001–003`) — required by the collector.
- Shared libraries used by both legacy payout API and new collector until import graph is untangled.

## Removal criteria (all required)

1. No runtime or **systemd** references to the legacy module.
2. No active **imports** from collector, reporting, or admin scripts.
3. No **deployed API** dependency on the legacy endpoint or job.
4. Replacement **SC-node payout-credit** design implemented and reviewed (PR G).
5. Documented **rollback plan** (restore units, schema, or feature flags).
6. **Tests updated** — legacy tests removed or rewritten for SC-node model.

## Non-goals

- No file deletion in PR D or this ADR update alone.
- No wallet RPC additions.
- No payout execution or transaction broadcast.
- No schema migrations for cleanup.
- No telemetry backfill of historical `sc_node_id IS NULL` rows.

## Consequences

- Legacy code remains until PR E/F criteria are met; operational risk is confusion, not immediate deletion.
- New work stays in `payouts/collector/` and read-only scripts until legacy paths are formally retired.

## Recommended PR sequence

| PR | Scope |
|----|--------|
| **PR D** | Inventory only (this ADR + matrix + script) — **no deletion** |
| **PR E** | Quarantine clearly unused demos/backfills/docs if safe |
| **PR F** | Remove or isolate legacy FastAPI / user-level payout path if no runtime dependency |
| **PR G** | Design SC-node-level payout-credit ledger |
| **PR H** | Guarded money movement (only after G + ops sign-off) |
