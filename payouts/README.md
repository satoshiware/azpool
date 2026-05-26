# Mining Payout Service

Initial scaffold for reward collection and payout settlement service.

## Pool telemetry collector (v0.1)

The support node runs a **separate telemetry collector** under `payouts/collector/` that polls `pool_sv2` monitoring APIs, stores channel snapshots, and computes accepted-work deltas in Postgres.

**Collector responsibilities:**

- Poll `/api/v1/health`, `/api/v1/clients`, and `/api/v1/clients/{client_id}/channels` on each configured pool instance.
- Store every observation in `pool_channel_snapshots`.
- Insert `pool_share_work_deltas` when counters increase between observations.

**Accepted-work formula (v0.1 telemetry):**

```
work_delta = current.share_work_sum - previous.share_work_sum
accepted_delta = current.shares_accepted - previous.shares_accepted
```

**Non-goals (collector must never):**

- Create payout credits, payout batches, or settlement rows.
- Call AZCoin Core wallet RPC or broadcast transactions.
- Treat monitoring counters as final immutable share events.
- Treat `user_identity` as a payout principal (audit/telemetry only).

**SC-node-first grouping:**

- Support node pays/credits **SC nodes**, not individual pool workers/users.
- Payout/credit math groups by `sc_node_id` only.
- `user_identity` is collector/audit telemetry; it should not appear in payout reports by default.
- Unknown identities remain unmapped and unpaid until explicitly mapped.
- Native long-term identity: `az/scnode/<sc_node_id>`.
- Temporary prefix mapping example: `baveetstudy.` → `sc-2` with `payout_enabled = false` (`sc-3` is inactive).
- Historical rows with `sc_node_id IS NULL` are not auto-backfilled in v0.1.

**Pool instance registry (DB-backed):**

- Active pool targets load from `pool_instances.monitoring_base_url` each timer run.
- `DATABASE_URL` remains required in collector.env.
- `POOL_INSTANCES` env JSON is temporary fallback only when DB registry is empty/unavailable.
- Disable a pool with `status='inactive'` or `monitoring_enabled=false` — no service restart needed.
- A Postgres advisory lock prevents overlapping one-shot runs (timer vs manual). Overlap skips safely with exit code 0.

A future payout/ledger service will read `pool_share_work_deltas` grouped by `sc_node_id`. Deploy via `deploy/systemd/azcoin-pool-collector.service` (one-shot) and `deploy/systemd/azcoin-pool-collector.timer` (30s interval). See `docs/runbooks/pool-monitoring-collector.md`.

**Read-only SC-node work summary:** `payouts/scripts/sc_node_work_summary.py` prints JSON totals grouped by `sc_node_id` (with unmapped `sc_node_id IS NULL` rows reported separately). It is read-only telemetry review only — not payout execution — and does not expose `user_identity` in default output. See `docs/runbooks/pool-monitoring-collector.md` for the `azledger` run command.

**Read-only pool-ledger admin:** `payouts/scripts/pool_ledger_admin_readonly.py` lists pool instances, SC nodes, identity mappings, and top unmapped identities (`user_identity` only in the `unmapped-identities` admin command). Not payout execution. See `docs/runbooks/pool-ledger-admin.md`.

**Legacy quarantine (PR E):** User-level payout demo and backfill scripts live under `payouts/legacy/scripts/` (not `payouts/scripts/`). They are preserved for audit only — not part of support-node SC-node telemetry/admin. See `payouts/legacy/README.md`.

**Legacy app boundary (PR F):** `payouts/app/` is legacy-candidate FastAPI user-level payout/settlement code — **not quarantined**. Documented in `payouts/app/README.md` and [payout-app-dependency-audit.md](../docs/inventory/payout-app-dependency-audit.md). Active support-node scripts remain under `payouts/scripts/`.

**SC-node payout address registry (PR G):** Migration `004_sc_node_payout_addresses.sql` adds `sc_node_payout_addresses` for per-SC-node payout destinations. Read-only admin command: `pool_ledger_admin_readonly.py payout-addresses`. **Payout execution is not implemented** — no wallet RPC, no coin sends. See [sc-node-payout-addresses.md](../docs/runbooks/sc-node-payout-addresses.md).

**Support-wallet reward listener (PR H):** Migration `005_support_wallet_reward_events.sql` records support-wallet `generate` / `immature` / `orphan` rows from read-only `azc listtransactions`. Manual scan: `support_wallet_reward_events.py` (dry-run by default; `--write` to upsert). Read-only admin: `pool_ledger_admin_readonly.py reward-events`. **Does not send coins, sign/broadcast transactions, or generate payout plans.** See [support-wallet-reward-listener.md](../docs/runbooks/support-wallet-reward-listener.md).

**SC-node credit ledger (PR I):** Migration `006_sc_node_credit_ledger.sql` allocates **mature** reward events to SC nodes by mapped pool work in an operator-selected coverage window. `sc_node_credit_ledger.py` preview/write/print (write requires explicit `--coverage-start`/`--coverage-end` or `--allow-default-coverage`). Read-only admin: `credit-runs`, `credit-run-details`. **`support_wallet_reward_events` is gross history, not wallet balance** — do not credit all mature rows blindly. **No sends, no `azc`.** Credit ledger entries are not payout authorization. See [sc-node-credit-ledger.md](../docs/runbooks/sc-node-credit-ledger.md).

**SC-node payout planner (PR J):** Migration `007_sc_node_payout_plans.sql` builds **no-send** draft payout plans from draft credits + active/default payout addresses, with operator `--trusted-balance-snapshot` and `--reserve-fraction` (default 0.50). `sc_node_payout_planner.py` preview/write-draft. Read-only admin: `payout-plans`, `payout-plan-details`. **Plans are proposals only — not wallet transactions.** See [sc-node-payout-planner.md](docs/sc-node-payout-planner.md).

**SC-node payout plan approval/preflight (PR K):** Migration `008_sc_node_payout_plan_approval_preflight.sql` adds approve/cancel/preflight workflow. `sc_node_payout_plan_review.py` with exact confirmation `APPROVE PAYOUT PLAN <id> NO SEND`. **Approval and preflight are not execution or spend authorization.** See [sc-node-payout-plan-review.md](docs/sc-node-payout-plan-review.md).

**SC-node payout test/regtest executor (PR L):** Migration `009_sc_node_payout_test_execution.sql` adds fake execution tables only. `sc_node_payout_test_executor.py` supports `preview`, `execute-fake` (`fake_regtest` + test wallet name), `mark-confirmed`, and `details`. **Does not move real coins, call AZCoin Core RPC, `azc`, or subprocess.** Read-only admin: `payout-test-executions`, `payout-test-execution-details`. Production wallet executor remains a future PR. See [sc-node-payout-test-executor.md](docs/sc-node-payout-test-executor.md).

**SC-node production payout preflight (PR M):** Migration `010_sc_node_payout_production_preflight.sql` adds production preflight audit tables. `sc_node_payout_production_preflight.py` supports `preview`, `record`, and `details`. **Starts the production-send safety track but does not send coins.** The only wallet RPC is read-only `getbalances` via explicit `--azc-bin` (e.g. `/tmp/azc` when `azc` is a shell alias). Default reserve is 50% of trusted balance; `--override-reserve` is explicit. Preflight records are not execution authorization. Read-only admin: `production-preflights`, `production-preflight-details`. See [sc-node-production-payout-preflight.md](docs/sc-node-production-payout-preflight.md).

**SC-node production payout executor (PR N):** Migration `011_sc_node_payout_production_execution.sql` adds production execution audit tables. `sc_node_payout_production_executor.py` supports `preview`, `execute-real`, `mark-confirmed`, and `details`. **First real coin send:** `sendtoaddress` only in `execute-real` after passed preflight, fresh `getbalances`, 50% reserve, exact confirmation phrase, and idempotency key. Manual operator-triggered only — no daemon/timer. No `sendmany`/raw tx/sign/passphrase. Read-only admin: `production-executions`, `production-execution-details`. See [sc-node-production-payout-executor.md](docs/sc-node-production-payout-executor.md).

**SC-node payout post-execution reconciliation (PR O):** Migration `012_sc_node_payout_reconciliation.sql` adds reconciliation audit tables. `sc_node_payout_reconciliation.py` supports `preview`, `record`, and `details`. **After execution only** — compares confirmed production execution rows, read-only source-wallet `gettransaction`, and optional receiver JSON export (no HTTP/bearer token). **Does not send coins or confirm executions.** Read-only admin: `payout-reconciliations`, `payout-reconciliation-details`. See [sc-node-payout-reconciliation.md](docs/sc-node-payout-reconciliation.md).

Migrations: `payouts/migrations/001_pool_telemetry_collector.sql`, `payouts/migrations/002_sc_node_identity_mapping.sql`, `payouts/migrations/003_pool_instance_registry.sql`, `payouts/migrations/004_sc_node_payout_addresses.sql`, `payouts/migrations/005_support_wallet_reward_events.sql`, `payouts/migrations/006_sc_node_credit_ledger.sql`, `payouts/migrations/007_sc_node_payout_plans.sql`, `payouts/migrations/008_sc_node_payout_plan_approval_preflight.sql`, `payouts/migrations/009_sc_node_payout_test_execution.sql`, `payouts/migrations/010_sc_node_payout_production_preflight.sql`, `payouts/migrations/011_sc_node_payout_production_execution.sql`, `payouts/migrations/012_sc_node_payout_reconciliation.sql`

---

1. Create virtual environment:
   - `python3 -m venv .venv`
   - `source .venv/bin/activate`
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Create env file:
   - `cp .env.example .env`
   - Edit `.env` and set required values for your environment
4. Start API:
   - `uvicorn app.main:app --reload`

Health check:
- `curl http://127.0.0.1:8000/health`

## Continuous Scheduler Mode

The service can run payout cycles continuously in-process using APScheduler.

Enable in `.env`:

```bash
SCHEDULER_ENABLED=true
SCHEDULER_INTERVAL_SECONDS=60
```

Then start API as usual:

```bash
uvicorn app.main:app --reload
```

While scheduler is running:
- each interval executes the same logic as `POST /settlements/run`,
- snapshots are polled (`poll_channels_once_with_blocks` when channels endpoint is configured),
- settlement and sender flow run,
- payout audit log keeps appending to `PAYOUT_AUDIT_LOG_PATH` (default `./logs/payout_audit.jsonl`).

## End-to-End Demo Run

Run a complete local demo that:
- maps channel_id to downstream user identity,
- writes two snapshots for translator channels,
- computes interval deltas,
- creates settlement + user payout rows,
- prints a payout-ready table.

```bash
python legacy/scripts/demo_interval_run.py
```

Optional args:

```bash
python legacy/scripts/demo_interval_run.py --db-path ./demo_payouts.db --interval-minutes 90 --reward-btc 0.01000000
```

### Live API Mode

To poll fresh translator data every run (instead of static embedded payloads):

```bash
python legacy/scripts/demo_interval_run.py \
   --mode live \
   --db-path ./demo_live.db \
   --interval-minutes 4 \
   --reward-btc 0.01000000 \
   --upstream-url http://192.168.38.155:8080/v1/translator/upstream/channels \
   --downstream-url http://192.168.38.155:8080/v1/translator/downstreams
```

Run it again after 2-4 minutes to see new snapshots and updated deltas/payout rows.

### Env-Driven Cadence (Demo)

You can control demo snapshot and payout cadence from environment values:

- `DEMO_PAYOUT_INTERVAL_MINUTES` (for example `2`)
- `DEMO_SNAPSHOT_INTERVAL_SECONDS` (for example `120`)
- `DEMO_LOOP_CYCLES` (how many live cycles in one run)
- `DEMO_DB_PATH`
- `DEMO_REWARD_BTC`

Example run using env defaults:

```bash
python legacy/scripts/demo_interval_run.py --mode live
```

Example explicit 2-minute payout with 2-minute snapshots:

```bash
python legacy/scripts/demo_interval_run.py --mode live --interval-minutes 2 --snapshot-interval-seconds 120 --loop-cycles 5
```

The output includes:
- settlement_id and interval window,
- user_share_delta and user_work_delta,
- payout_fraction and amount_btc,
- translator_total_work for that payout interval.

## Postgres Schema Migrations

Local Postgres schema bootstrapping for the payout ledger:

```bash
cd ledger
docker compose -f docker-compose.postgres.yml up -d

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

alembic -c alembic.ini upgrade head
```

Inspect the schema:

```bash
docker compose -f docker-compose.postgres.yml exec postgres psql -U azledger -d azcoin_ledger_dev -c "\dt"
docker compose -f docker-compose.postgres.yml exec postgres psql -U azledger -d azcoin_ledger_dev -c "\d settlement_windows"
docker compose -f docker-compose.postgres.yml exec postgres psql -U azledger -d azcoin_ledger_dev -c "\d settlement_blocks"
```

Roll back and stop:

```bash
alembic -c alembic.ini downgrade base
docker compose -f docker-compose.postgres.yml down
```

## Historical Postgres Shadow Backfill

Dry-run one historical settlement into the Postgres shadow ledger:

```bash
POSTGRES_LEDGER_DATABASE_URL=postgresql+psycopg://azledger:azledger_dev_password@localhost:5432/azcoin_ledger_dev \
python legacy/scripts/backfill_postgres_shadow.py --settlement-id 49
```

Write a bounded range only when you explicitly want inserts/upserts:

```bash
POSTGRES_LEDGER_DATABASE_URL=postgresql+psycopg://azledger:azledger_dev_password@localhost:5432/azcoin_ledger_dev \
python legacy/scripts/backfill_postgres_shadow.py --start-id 40 --end-id 49 --write
```

## Step 7 Candidate Read Cutover

After sqlite_settlement_id backfill is complete and shadow parity is clean, enable Postgres candidate reads for public settlement endpoints:

```bash
POSTGRES_LEDGER_READS_ENABLED=true
POSTGRES_LEDGER_READ_MODE=postgres_shadow_candidate
POSTGRES_LEDGER_READ_ALLOWED_ENDPOINTS=settlement_history,settlement_detail
POSTGRES_LEDGER_READ_REQUIRE_SHADOW_MATCH=true
POSTGRES_LEDGER_READ_FALLBACK_TO_SQLITE=true
```

These settings keep SQLite fallback active while candidate reads are validated in production.

## Step 8 Primary Session Cutover

After candidate reads are stable, switch the app session source to Postgres:

```bash
POSTGRES_PRIMARY_SESSION_ENABLED=true
POSTGRES_PRIMARY_SESSION_FALLBACK_TO_SQLITE=true
```

For strict mode (fail fast if Postgres is unavailable):

```bash
POSTGRES_PRIMARY_SESSION_ENABLED=true
POSTGRES_PRIMARY_SESSION_FALLBACK_TO_SQLITE=false
```

## Step 9 SQLite Retirement Mode

After parity is stable over multiple cycles, disable SQLite runtime writes and fallbacks:

```bash
SQLITE_RETIREMENT_MODE_ENABLED=true
SQLITE_RUNTIME_WRITES_ENABLED=false
POSTGRES_PRIMARY_SESSION_ENABLED=true
POSTGRES_PRIMARY_SESSION_FALLBACK_TO_SQLITE=false
POSTGRES_SETTLEMENT_ENGINE_ENABLED=true
POSTGRES_SENDER_ENABLED=true
POSTGRES_LEDGER_READ_FALLBACK_TO_SQLITE=false
```

When retirement mode is enabled, the service enforces these prerequisites and fails fast if they are not satisfied.
