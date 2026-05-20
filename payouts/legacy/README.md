# Legacy payout-ledger tooling (quarantined)

This directory contains **quarantined legacy payout-ledger scripts** moved out of `payouts/scripts/` so they are not confused with the active support-node path.

## Not part of the active path

The support node’s current production work is:

- Pool telemetry collector (`payouts/collector/app/*`)
- Read-only SC-node work summary (`payouts/scripts/sc_node_work_summary.py`)
- Read-only pool-ledger admin (`payouts/scripts/pool_ledger_admin_readonly.py`)
- Telemetry migrations (`payouts/migrations/001–003`)

Nothing under `payouts/legacy/` is used by collector systemd, read-only admin/report scripts, or SC-node telemetry grouping.

## Purpose of quarantine

Scripts here are **preserved for audit and history only**. They were moved in PR E (quarantine), not deleted.

## Safety rules

- **Do not use** these scripts for production payout execution on the support node.
- Support node credits/pays **SC nodes** (`sc_node_id`) only — not individual pool worker `user_identity` values.
- Worker/customer splits happen **below** the SC node.
- Anything touching **SQLite**, **settlement**, **user-level payouts**, or **Postgres shadow backfill** requires explicit review before use.
- Future payout/credit design must be **SC-node-level**, not worker/user-level settlement.

## Contents

| Script | Former path |
|--------|-------------|
| `scripts/demo_interval_run.py` | `payouts/scripts/demo_interval_run.py` |
| `scripts/backfill_postgres_shadow.py` | `payouts/scripts/backfill_postgres_shadow.py` |
| `scripts/backfill_sqlite_settlement_mapping.py` | `payouts/scripts/backfill_sqlite_settlement_mapping.py` |

See [payout-ledger-file-inventory.md](../../docs/inventory/payout-ledger-file-inventory.md) and [ADR-pool-ledger-legacy-cleanup-plan.md](../../docs/adr/ADR-pool-ledger-legacy-cleanup-plan.md).
