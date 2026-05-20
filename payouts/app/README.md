# Legacy-candidate payout application

This directory contains the **legacy user-level payout/settlement** FastAPI service, SQLite ledger, Postgres shadow ledger, wallet sender paths, and related helpers. It predates the support-node **SC-node-first** telemetry and admin architecture.

## Not the active support-node path

The active support-node path is:

- `payouts/collector/app/*` — pool telemetry collector
- `payouts/scripts/sc_node_work_summary.py` — read-only SC-node work summary
- `payouts/scripts/pool_ledger_admin_readonly.py` — read-only pool-ledger admin
- `payouts/migrations/001–003` — telemetry, SC-node identity, pool registry

**This directory is not** part of collector systemd, read-only reporting, or SC-node admin tooling.

## Legacy-candidate status (PR F)

Files here are **preserved pending dependency audit and staged quarantine**. They have **not** been moved to `payouts/legacy/` because they remain referenced by extensive legacy tests and may still represent deployed historical behavior.

See:

- [payout-app-dependency-audit.md](../docs/inventory/payout-app-dependency-audit.md)
- [payout-ledger-file-inventory.md](../docs/inventory/payout-ledger-file-inventory.md)
- [ADR-pool-ledger-legacy-cleanup-plan.md](../docs/adr/ADR-pool-ledger-legacy-cleanup-plan.md)

## Architecture rules

- Support node credits/pays **SC nodes** (`sc_node_id`) only — not individual pool worker `user_identity` values.
- `user_identity` is telemetry/audit input only; payout math must group by `sc_node_id`.
- Worker/customer splits happen **below** the SC node.
- **Future payout automation** must be redesigned around **SC-node-level payout credits**, not worker/user-level settlement in this tree.

## Do not run casually

**Do not use** modules here for production payout execution without explicit review.

**Warning:** Code touching **settlement**, **sender**, **wallet RPC**, or **UserPayout** rows can move money or mutate payout state. Standalone demos and backfills that import `app.*` live under `payouts/legacy/scripts/` and carry the same caution.

Do **not** quarantine or delete this tree until:

1. Runtime/service/deploy references are verified unused on the support node.
2. A replacement **SC-node-level** payout-credit design exists.
3. A documented rollback plan is approved.
