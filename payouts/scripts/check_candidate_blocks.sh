#!/usr/bin/env bash
set -euo pipefail

LIMIT="${1:-20}"
cd /opt/azcoin-ledger/app/ledger

set -a
source /etc/azcoin-ledger/ledger.env
set +a

/opt/azcoin-ledger/app/ledger/.venv/bin/python - <<PY
import os
from sqlalchemy import create_engine, text

limit = int("${LIMIT}")
engine = create_engine(os.environ["POSTGRES_LEDGER_DATABASE_URL"], pool_pre_ping=True)

with engine.connect() as conn:
    total = conn.execute(text("select count(*) from translator_candidate_blocks")).scalar()

    rows = conn.execute(text("""
        select
            id,
            found_time,
            found_time_unix,
            blockhash,
            worker_identity,
            channel_id,
            job_id,
            ntime,
            nonce,
            source,
            proof_type,
            created_at,
            extract(epoch from (created_at - found_time)) as ingest_delay_seconds
        from translator_candidate_blocks
        order by found_time desc, id desc
        limit :limit
    """), {"limit": limit}).fetchall()

print({"translator_candidate_blocks_count": total, "showing": limit})
for row in rows:
    print(dict(row._mapping))
PY
