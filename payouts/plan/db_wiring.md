**Current Reality**
Right now the engine still runs on SQLite because:
1. Every runtime session is opened from `DB_PATH` in main.py.
2. Settlement math reads SQLite snapshots via `compute_user_contribution_deltas(...)` in settlement.py and delta.py.
3. Sender queue/state is SQLite-only in sender.py and models.py.
4. Carry and work-accrual state are SQLite-only in models.py and models.py.
5. Public Postgres reads are still blocked by the missing settlement-id mapping in main.py.

**Implementation Order**
Do it in this order, otherwise you will end up with a half-cut system again.

1. Add missing Postgres runtime tables.
You need Postgres equivalents for:
`carry_state`, `work_accrual_bucket`, `payout_events`, and either `block_counter_state` or a replacement using `service_cursors`.
These exist in SQLite models now but not in Postgres schema:
models.py
postgres_schema.py

2. Add a durable public settlement mapping in Postgres.
Add something like `sqlite_settlement_id` to `settlement_windows`, make it unique, backfill it, and populate it on every shadow write.
That is the missing field the read path already expects in:
main.py
main.py

3. Start writing raw runtime snapshots directly to Postgres during polling.
Right now Postgres has `raw_miner_snapshots`, but the runtime polling path still only writes SQLite `MetricSnapshot`.
You need to mirror or move `poll_channels_once_with_blocks(...)` and `poll_metrics_once(...)` so they write Postgres `raw_miner_snapshots` too, ideally as the canonical write path.
Current SQLite write path:
poller.py
Postgres repository capability already exists:
postgres_repositories.py

4. Port contribution delta computation to Postgres.
You need a Postgres implementation of `compute_user_contribution_deltas(...)` that matches SQLite semantics exactly:
baseline before window, current inside window, reset detection, positive-only accumulation.
Current logic to port:
delta.py

5. Port settlement execution off SQLite ORM models.
Right now `run_settlement(...)` is tightly bound to SQLite ORM models `Settlement`, `UserPayout`, `CarryState`, `WorkAccrualBucket`.
You need either:
a. a new Postgres-backed settlement service using `PostgresLedgerRepository`, or
b. a repository abstraction used by both SQLite and Postgres.
This is the real cutover point.
Current settlement logic:
settlement.py

6. Port payout event sending to Postgres.
Sender currently creates and updates SQLite `PayoutEvent` rows and marks SQLite `UserPayout` rows as sent.
That must move to Postgres before SQLite can stop being authoritative.
Current sender:
sender.py

7. Switch reads to Postgres after audit parity is clean.
Once `sqlite_settlement_id` exists and backfill is done:
set `POSTGRES_LEDGER_READS_ENABLED=true`
set `POSTGRES_LEDGER_READ_MODE=postgres_shadow_candidate`
set `POSTGRES_LEDGER_READ_ALLOWED_ENDPOINTS=settlement_history,settlement_detail`
and keep `POSTGRES_LEDGER_READ_REQUIRE_SHADOW_MATCH=true`
That gives you read cutover with safety fallback first.

8. Switch the engine’s primary session from SQLite to Postgres.
Only after steps 1–7 are done should `_new_session()` stop opening SQLite.
That change is in:
main.py

9. Keep SQLite in dual-write mode briefly, then retire it.
For a safe rollout:
a. dual-write snapshots/settlements/payouts to both stores,
b. compare parity through the existing shadow audit endpoints,
c. flip reads,
d. flip settlement source,
e. disable SQLite writes only after stable parity.

**Practical Rollout Plan**
Use this rollout order in production:

1. Add missing Postgres schema and migrations.
2. Backfill historical SQLite settlements into Postgres.
3. Add `sqlite_settlement_id` mapping and backfill it.
4. Dual-write snapshots to Postgres.
5. Implement Postgres delta computation and test parity.
6. Implement Postgres settlement execution and accrual/carry parity.
7. Implement Postgres payout-event queue and sender.
8. Enable Postgres reads in candidate mode.
9. Verify audit parity over many cycles.
10. Change `_new_session()` and settlement engine to Postgres.
11. Leave SQLite fallback in place for one release window.
12. Remove SQLite as source of truth only after stable production validation.

So the direct answer is: to make Postgres the real DB, you need a source-of-truth migration across polling, delta computation, settlement execution, accrual state, sender state, and read mapping. Right now only the shadow/replica side is complete.

If you want, I can turn this into a concrete Phase E implementation plan with exact files, schema additions, and commit-sized tasks.