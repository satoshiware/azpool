# ADR: Pool ledger legacy cleanup plan

**Status:** Accepted (inventory PR D — no code deletion)

**Date:** 2026-05-19

## Decision

**Maintain** the current collector, read-only reporting, and read-only admin paths as **ACTIVE**. **Classify** old user-level payout, settlement, SQLite, and FastAPI service code as **LEGACY-CANDIDATE**. **Require** file-level inventory verification (see [payout-ledger-file-inventory.md](../inventory/payout-ledger-file-inventory.md)) before any deletion or quarantine.

**PR D (`feature/payout-ledger-legacy-inventory-v0`)** added inventory documentation and a read-only inventory script only (no code moves).

**PR E (`feature/payout-ledger-legacy-quarantine-plan-v0`)** quarantines standalone legacy scripts under `payouts/legacy/scripts/` via `git mv` only — **no deletion**, no change to active collector/admin/reporting behavior.

**PR F (`feature/payout-app-dependency-audit-v0`)** audits `payouts/app/*` dependencies, adds `payouts/app/README.md` boundary markers, and **does not move or delete** app code.

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

## PR E quarantine decision

Standalone legacy scripts with **no** active `deploy/` or systemd references were moved (not deleted) to reduce accidental use while preserving audit history:

| New path | Former path |
|----------|-------------|
| `payouts/legacy/scripts/demo_interval_run.py` | `payouts/scripts/demo_interval_run.py` |
| `payouts/legacy/scripts/backfill_postgres_shadow.py` | `payouts/scripts/backfill_postgres_shadow.py` |
| `payouts/legacy/scripts/backfill_sqlite_settlement_mapping.py` | `payouts/scripts/backfill_sqlite_settlement_mapping.py` |

- **No code deleted.**
- **No change** to `payouts/collector/app/*`, read-only admin/report scripts, or support-node telemetry migrations.
- **`payouts/legacy/README.md`** documents quarantine rules and SC-node-first architecture.
- **`payouts/app/*` remains untouched** pending a separate runtime/API dependency review (PR F).
- `payouts/tests/test_postgres_shadow_backfill.py` updated in PR E to load the script from `payouts/legacy/scripts/backfill_postgres_shadow.py`.

Preflight: grep across `deploy/`, `docs/runbooks`, and collector paths found **no** production references to the old `payouts/scripts/` paths for these three files. References remain in `payouts/README.md` (updated to `legacy/scripts/`) and legacy `payouts/tests/` (backfill path fixed in PR E).

## PR F dependency audit decision

`payouts/app/` remains **in place**. PR F adds:

- [payout-app-dependency-audit.md](../inventory/payout-app-dependency-audit.md) — static/runtime/test dependency findings
- `payouts/app/README.md` — legacy-candidate boundary warning
- `payouts/scripts/audit_payout_app_dependencies.py` — read-only JSON audit helper

**No runtime behavior changed. No files moved.**

Findings:

- `payouts/app` is **not** imported by the collector or support-node systemd units.
- `payouts/app` **is** heavily imported by `payouts/tests/*`, quarantined legacy scripts, and **`payouts/scripts/run_translator_sv1_capture_proxy.py`** (active non-legacy script importing Postgres/translator modules).
- Audit inbound counts **exclude** generated/cache files (`__pycache__/`, `.pytest_cache/`, `.pyc`, `.pyo`, `.so`, `.git/`, `.venv/`).
- **Do not quarantine** `payouts/app` until replacement SC-node payout-credit design (PR G–I), translator runtime ownership is decided, and production verification complete.

**Next architectural step:** SC-node payout address registry (PR G) and read-only support-wallet reward listener (PR H). Future removal/quarantine requires replacement SC-node ledger and verified no production dependency.

## Inventory artifact (PR D / PR E / PR F)

| Artifact | Purpose |
|----------|---------|
| [docs/inventory/payout-ledger-file-inventory.md](../inventory/payout-ledger-file-inventory.md) | Human-readable matrix: classification, evidence, proposed action, risk |
| [docs/inventory/payout-app-dependency-audit.md](../inventory/payout-app-dependency-audit.md) | PR F `payouts/app` dependency audit |
| `payouts/scripts/audit_payout_app_dependencies.py` | Read-only JSON audit of `payouts/app` inbound refs |
| `payouts/app/README.md` | Legacy boundary marker (PR F) |
| `payouts/scripts/inventory_payout_ledger_files.py` | Read-only JSON scan (paths, keywords, suggested classification) |
| `payouts/collector/tests/test_payout_ledger_inventory.py` | Unit tests for inventory classification rules |
| `payouts/collector/tests/test_payout_app_dependency_audit.py` | Unit tests for app dependency audit script |

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
| `payouts/legacy/scripts/demo_interval_run.py` | Demo / interval payout runner (quarantined PR E) |
| `payouts/legacy/scripts/backfill_postgres_shadow.py` | Shadow backfill (quarantined PR E) |
| `payouts/legacy/scripts/backfill_sqlite_settlement_mapping.py` | SQLite settlement mapping backfill (quarantined PR E) |
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
| **PR D** | Inventory only (matrix + script) — **no deletion** |
| **PR E** | Quarantine standalone legacy scripts → `payouts/legacy/scripts/` — **no deletion** |
| **PR F** | `payouts/app` dependency audit + boundary README — no moves |
| **PR G** | SC-node payout address registry spec/schema |
| **PR H** | Read-only support-wallet reward listener |
| **PR I** | SC-node credit ledger (no wallet sends) |
| **PR J** | Payout plan generator (no wallet sends) |
| **PR K** | Guarded dry-run wallet payout execution (separate approval) |
