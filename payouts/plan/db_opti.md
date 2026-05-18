## Plan: Postgres Snapshot Compaction

Implement a Postgres-only compaction path so each settled interval writes one summary header plus per-miner summary rows, then prunes old raw snapshots with a keep-last-3-windows retention policy. This runs immediately after settlement shadow write, including deferred intervals, and stays idempotent on retries.

**Steps**
1. Define interval contract and retention behavior for half-open windows: start inclusive, end exclusive.
2. Phase A - schema:
3. Add a new summary header table named summary_snapshot with settlement/window metadata and aggregate totals.
4. Add child table summary_snapshot_miner keyed to summary_snapshot id with worker identity/name, channel id, and share/work aggregates.
5. Add uniqueness/indexing for idempotent upserts and fast window reads.
6. Create Alembic migration for both tables and constraints.
7. Phase B - repository methods:
8. Add aggregation query over raw_miner_snapshots for one settlement contribution window.
9. Add upsert methods for summary_snapshot and summary_snapshot_miner.
10. Add retention prune methods that keep only latest 3 windows and delete older data.
11. Prune miner_work_deltas together with old raw snapshots to avoid FK blockers.
12. Phase C - orchestration hook:
13. In settlement cycle shadow-write flow, trigger compaction immediately after successful Postgres settlement write.
14. Run compaction for completed and deferred settlements.
15. Ensure retry safety: no duplicate summary rows and no over-delete.
16. Include compaction stats in settlement response/audit payload.
17. Phase D - tests and validation:
18. Add repository unit tests for aggregation, upsert idempotency, retention prune ordering, and boundary handling.
19. Add integration tests for settlement -> summary write -> prune behavior.
20. Add tests for deferred settlement compaction and rerun idempotency.
21. Run payout/accrual regression tests to verify no reward math behavior changes.

**Relevant files**
- ledger/alembic/versions — add migration for summary tables.
- ledger/app/postgres_schema.py — define summary_snapshot and summary_snapshot_miner tables.
- ledger/app/postgres_repositories.py — aggregation/upsert/prune methods.
- ledger/app/main.py — hook compaction after shadow settlement write.
- ledger/tests/test_postgres_repositories.py — repository coverage.
- ledger/tests/test_postgres_shadow_write.py — integration coverage.
- ledger/tests/test_health.py — settlement API behavior assertions.
- ledger/docs/payout-ledger-postgres-schema.md — runtime/retention docs update.

**Verification**
1. Repository tests: interval aggregation correctness and idempotent upsert.
2. Integration tests: summary rows written, raw snapshots pruned beyond last 3 windows.
3. Deferred test: deferred windows still compact.
4. Retry test: same settlement rerun does not duplicate summary or prune extra.
5. Regression tests: payout ratios, accrual carry-forward, and block-event reward behavior unchanged.

**Decisions captured**
- Summary model: two tables (summary_snapshot + summary_snapshot_miner).
- Trigger: immediate after settlement shadow write.
- Deferred intervals: compact them too.
- Retention: keep latest 3 interval windows.
- Delta retention: prune matching miner_work_deltas with old raw snapshots.
- Window semantics: half-open start inclusive/end exclusive.

This plan is saved in /memories/session/plan.md and ready for handoff/implementation.