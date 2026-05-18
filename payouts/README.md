# Mining Payout Service

Initial scaffold for reward collection and payout settlement service.

## Quickstart

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
python scripts/demo_interval_run.py
```

Optional args:

```bash
python scripts/demo_interval_run.py --db-path ./demo_payouts.db --interval-minutes 90 --reward-btc 0.01000000
```

### Live API Mode

To poll fresh translator data every run (instead of static embedded payloads):

```bash
python scripts/demo_interval_run.py \
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
python scripts/demo_interval_run.py --mode live
```

Example explicit 2-minute payout with 2-minute snapshots:

```bash
python scripts/demo_interval_run.py --mode live --interval-minutes 2 --snapshot-interval-seconds 120 --loop-cycles 5
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
python scripts/backfill_postgres_shadow.py --settlement-id 49
```

Write a bounded range only when you explicitly want inserts/upserts:

```bash
POSTGRES_LEDGER_DATABASE_URL=postgresql+psycopg://azledger:azledger_dev_password@localhost:5432/azcoin_ledger_dev \
python scripts/backfill_postgres_shadow.py --start-id 40 --end-id 49 --write
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
