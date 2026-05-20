from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
import sys
from typing import Any

from sqlalchemy import select, update

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_settings
from app.db import make_engine, make_session_factory
from app.models import Settlement
from app.postgres_db import make_postgres_engine, make_postgres_session_factory
from app.postgres_schema import settlement_windows
from app.postgres_shadow_compare import load_sqlite_settlement_context


def _to_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _load_sqlite_settlement_ids(
    sqlite_session,
    *,
    settlement_id: int | None,
    start_id: int | None,
    end_id: int | None,
    limit: int | None,
) -> list[int]:
    statement = select(Settlement.id).order_by(Settlement.id.asc())
    if settlement_id is not None:
        statement = statement.where(Settlement.id == settlement_id)
    else:
        if start_id is not None:
            statement = statement.where(Settlement.id >= start_id)
        if end_id is not None:
            statement = statement.where(Settlement.id <= end_id)
        if limit is not None:
            statement = statement.limit(limit)
    return [int(row[0]) for row in sqlite_session.execute(statement).all()]


def run_backfill(
    *,
    settlement_id: int | None,
    start_id: int | None,
    end_id: int | None,
    limit: int | None,
    write: bool,
) -> dict[str, Any]:
    settings = load_settings()

    sqlite_engine = make_engine(settings.db_path)
    sqlite_session_factory = make_session_factory(sqlite_engine)

    postgres_url = os.getenv("POSTGRES_LEDGER_DATABASE_URL", "").strip()
    if not postgres_url:
        raise RuntimeError("POSTGRES_LEDGER_DATABASE_URL must be set")

    pg_engine = make_postgres_engine(postgres_url)
    pg_session_factory = make_postgres_session_factory(pg_engine)

    report: dict[str, Any] = {
        "mode": "write" if write else "dry_run",
        "sqlite_db_path": settings.db_path,
        "processed": 0,
        "mapped": 0,
        "already_mapped": 0,
        "missing_postgres_window": 0,
        "sqlite_context_missing": 0,
        "conflicts": 0,
        "rows": [],
    }

    with sqlite_session_factory() as sqlite_session:
        settlement_ids = _load_sqlite_settlement_ids(
            sqlite_session,
            settlement_id=settlement_id,
            start_id=start_id,
            end_id=end_id,
            limit=limit,
        )

        with pg_session_factory() as pg_session:
            for sid in settlement_ids:
                report["processed"] += 1
                context = load_sqlite_settlement_context(sqlite_session, sid)
                if context is None:
                    report["sqlite_context_missing"] += 1
                    report["rows"].append(
                        {
                            "sqlite_settlement_id": sid,
                            "status": "sqlite_context_missing",
                        }
                    )
                    continue

                work_window_start = _to_utc_aware(context.work_window_start)
                work_window_end = _to_utc_aware(context.work_window_end)

                pg_window = pg_session.execute(
                    select(settlement_windows).where(
                        settlement_windows.c.work_window_start == work_window_start,
                        settlement_windows.c.work_window_end == work_window_end,
                    )
                ).mappings().first()

                if pg_window is None:
                    report["missing_postgres_window"] += 1
                    report["rows"].append(
                        {
                            "sqlite_settlement_id": sid,
                            "status": "missing_postgres_window",
                            "work_window_start": work_window_start,
                            "work_window_end": work_window_end,
                        }
                    )
                    continue

                existing_for_sid = pg_session.execute(
                    select(settlement_windows).where(
                        settlement_windows.c.sqlite_settlement_id == sid
                    )
                ).mappings().first()

                pg_settlement_window_id = int(pg_window["id"])
                existing_mapped_sid = pg_window["sqlite_settlement_id"]

                if existing_for_sid is not None and int(existing_for_sid["id"]) != pg_settlement_window_id:
                    report["conflicts"] += 1
                    report["rows"].append(
                        {
                            "sqlite_settlement_id": sid,
                            "status": "conflict_sid_points_to_different_window",
                            "expected_window_id": pg_settlement_window_id,
                            "actual_window_id": int(existing_for_sid["id"]),
                            "work_window_start": work_window_start,
                            "work_window_end": work_window_end,
                        }
                    )
                    continue

                if existing_mapped_sid is None:
                    if write:
                        pg_session.execute(
                            update(settlement_windows)
                            .where(settlement_windows.c.id == pg_settlement_window_id)
                            .values(sqlite_settlement_id=sid)
                        )
                    report["mapped"] += 1
                    report["rows"].append(
                        {
                            "sqlite_settlement_id": sid,
                            "status": "mapped" if write else "would_map",
                            "postgres_settlement_window_id": pg_settlement_window_id,
                            "work_window_start": work_window_start,
                            "work_window_end": work_window_end,
                        }
                    )
                    continue

                if int(existing_mapped_sid) == sid:
                    report["already_mapped"] += 1
                    report["rows"].append(
                        {
                            "sqlite_settlement_id": sid,
                            "status": "already_mapped",
                            "postgres_settlement_window_id": pg_settlement_window_id,
                        }
                    )
                    continue

                report["conflicts"] += 1
                report["rows"].append(
                    {
                        "sqlite_settlement_id": sid,
                        "status": "conflict_window_already_mapped_to_other_sid",
                        "postgres_settlement_window_id": pg_settlement_window_id,
                        "existing_sqlite_settlement_id": int(existing_mapped_sid),
                        "work_window_start": work_window_start,
                        "work_window_end": work_window_end,
                    }
                )

            if write:
                pg_session.commit()

    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill settlement_windows.sqlite_settlement_id by matching work windows."
    )
    parser.add_argument("--settlement-id", type=int, default=None)
    parser.add_argument("--start-id", type=int, default=None)
    parser.add_argument("--end-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Apply updates. Without this flag the script runs in dry-run mode.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = run_backfill(
        settlement_id=args.settlement_id,
        start_id=args.start_id,
        end_id=args.end_id,
        limit=args.limit,
        write=bool(args.write),
    )
    print(json.dumps(report, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
