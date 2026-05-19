from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.db import Base, make_engine, make_session_factory
from app.delta import compute_user_contribution_deltas
from app.models import MetricSnapshot
from app.postgres_delta import compute_user_contribution_deltas_postgres


class _FakePostgresRepository:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def list_raw_miner_snapshot_counters_up_to(self, *, period_end: datetime) -> list[dict]:
        return [row for row in self._rows if row["captured_at"] <= period_end]


@pytest.fixture
def session(tmp_path: Path):
    db_file = tmp_path / "postgres_delta_parity.db"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)
    with Session() as s:
        yield s


def _add_snapshot(
    session,
    identity: str,
    total: int,
    created_at: datetime,
    *,
    channel_id: int | None = None,
    work_total: Decimal | float | int = Decimal("0"),
) -> None:
    session.add(
        MetricSnapshot(
            channel_id=channel_id,
            identity=identity,
            accepted_shares_total=total,
            accepted_work_total=work_total,
            shares_rejected_total=0,
            created_at=created_at,
        )
    )


def _build_raw_rows(rows: list[MetricSnapshot]) -> list[dict]:
    raw_rows: list[dict] = []
    for row in rows:
        created_at = row.created_at
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            created_at = created_at.replace(tzinfo=UTC)
        raw_rows.append(
            {
                "identity": row.identity,
                "channel_id": row.channel_id,
                "accepted_shares_total": row.accepted_shares_total,
                "accepted_work_total": Decimal(str(row.accepted_work_total or 0)),
                "captured_at": created_at,
            }
        )
    raw_rows.sort(
        key=lambda item: (
            str(item["identity"]),
            -1 if item["channel_id"] is None else int(item["channel_id"]),
            item["captured_at"],
        )
    )
    return raw_rows


def test_postgres_delta_matches_sqlite_semantics(session) -> None:
    base = datetime(2026, 1, 1, 0, 0, 0)
    start = base + timedelta(minutes=10)
    end = base + timedelta(minutes=30)

    _add_snapshot(session, "alice.m1", 10, base + timedelta(minutes=1), channel_id=1, work_total=100)
    _add_snapshot(session, "alice.m1", 13, base + timedelta(minutes=12), channel_id=1, work_total=145)
    _add_snapshot(session, "alice.m1", 16, base + timedelta(minutes=25), channel_id=1, work_total=160)

    _add_snapshot(session, "bob.m1", 8, base + timedelta(minutes=2), channel_id=2, work_total=80)
    _add_snapshot(session, "bob.m1", 2, base + timedelta(minutes=14), channel_id=2, work_total=5)
    _add_snapshot(session, "bob.m1", 5, base + timedelta(minutes=25), channel_id=2, work_total=33)

    _add_snapshot(session, "bob.m2", 1, base + timedelta(minutes=11), channel_id=3, work_total=10)
    _add_snapshot(session, "bob.m2", 4, base + timedelta(minutes=28), channel_id=3, work_total=40)

    _add_snapshot(session, "badidentity", 3, base + timedelta(minutes=1), channel_id=4, work_total=12)
    _add_snapshot(session, "badidentity", 9, base + timedelta(minutes=18), channel_id=4, work_total=48)
    session.commit()

    sqlite_contributions = compute_user_contribution_deltas(session, start, end)

    metric_rows = (
        session.query(MetricSnapshot)
        .order_by(MetricSnapshot.identity.asc(), MetricSnapshot.channel_id.asc(), MetricSnapshot.created_at.asc())
        .all()
    )
    raw_rows = _build_raw_rows(metric_rows)
    repo = _FakePostgresRepository(raw_rows)

    postgres_contributions = compute_user_contribution_deltas_postgres(
        repo,
        start.replace(tzinfo=UTC),
        end.replace(tzinfo=UTC),
    )

    assert sorted(sqlite_contributions.keys()) == sorted(postgres_contributions.keys())
    for username, sqlite_item in sqlite_contributions.items():
        pg_item = postgres_contributions[username]
        assert pg_item.share_delta == sqlite_item.share_delta
        assert pg_item.work_delta == sqlite_item.work_delta


def test_postgres_delta_handles_missing_baseline_and_resets() -> None:
    start = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    end = datetime(2026, 1, 1, 10, 30, tzinfo=UTC)

    rows = [
        {
            "identity": "charlie.r1",
            "channel_id": 1,
            "accepted_shares_total": 7,
            "accepted_work_total": Decimal("70"),
            "captured_at": datetime(2026, 1, 1, 10, 5, tzinfo=UTC),
        },
        {
            "identity": "charlie.r1",
            "channel_id": 1,
            "accepted_shares_total": 3,
            "accepted_work_total": Decimal("10"),
            "captured_at": datetime(2026, 1, 1, 10, 10, tzinfo=UTC),
        },
        {
            "identity": "charlie.r1",
            "channel_id": 1,
            "accepted_shares_total": 8,
            "accepted_work_total": Decimal("55"),
            "captured_at": datetime(2026, 1, 1, 10, 20, tzinfo=UTC),
        },
    ]

    repo = _FakePostgresRepository(rows)
    contributions = compute_user_contribution_deltas_postgres(repo, start, end)

    assert set(contributions.keys()) == {"charlie"}
    assert contributions["charlie"].share_delta == 5
    assert contributions["charlie"].work_delta == Decimal("45")
