from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from app.db import Base, make_engine, make_session_factory
from app.main import app
from app.models import Settlement, SnapshotBlock, User, UserPayout
from app.postgres_shadow_compare import compare_postgres_shadow_settlement


class FakeCompareRepository:
    def __init__(
        self,
        *,
        expected_work_window_start: datetime,
        expected_work_window_end: datetime,
        settlement_row: dict[str, object] | None,
        credit_rows: list[dict[str, object]] | None = None,
        block_rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.expected_work_window_start = expected_work_window_start
        self.expected_work_window_end = expected_work_window_end
        self.settlement_row = settlement_row
        self.credit_rows = credit_rows or []
        self.block_rows = block_rows or []
        self.read_calls: list[str] = []

    def get_settlement_window_by_range(
        self,
        *,
        work_window_start: datetime,
        work_window_end: datetime,
    ) -> dict[str, object] | None:
        self.read_calls.append("get_settlement_window_by_range")
        assert work_window_start == self.expected_work_window_start
        assert work_window_end == self.expected_work_window_end
        return self.settlement_row

    def list_settlement_user_credits_with_users(self, settlement_id: int) -> list[dict[str, object]]:
        self.read_calls.append(f"list_settlement_user_credits_with_users:{settlement_id}")
        return list(self.credit_rows)

    def list_settlement_blocks(self, settlement_id: int) -> list[dict[str, object]]:
        self.read_calls.append(f"list_settlement_blocks:{settlement_id}")
        return list(self.block_rows)


class ReadOnlyGuardRepository(FakeCompareRepository):
    def __getattr__(self, name: str):
        if name.startswith(("upsert_", "create_", "set_", "link_")):
            raise AssertionError(f"Unexpected write call attempted: {name}")
        raise AttributeError(name)


def _seed_sqlite_settlement(tmp_path):
    db_file = tmp_path / "shadow_compare.db"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)

    with Session() as session:
        user = User(username="alice")
        session.add(user)
        session.flush()

        settlement = Settlement(
            status="completed",
            period_start=datetime(2026, 1, 1, 0, 0, 0),
            period_end=datetime(2026, 1, 1, 0, 10, 0),
            total_shares=5,
            total_work=Decimal("100.00000000"),
            pool_reward_btc=Decimal("0.50000000"),
        )
        session.add(settlement)
        session.flush()

        session.add(
            UserPayout(
                settlement_id=settlement.id,
                user_id=user.id,
                contribution_value=Decimal("100.00000000"),
                payout_fraction=Decimal("1.000000000000"),
                amount_btc=Decimal("0.50000000"),
                idempotency_key=f"settlement-{settlement.id}-user-{user.id}",
                status="pending",
            )
        )
        session.add(
            SnapshotBlock(
                found_at=datetime(2026, 1, 1, 0, 5, 0),
                channel_id=7,
                worker_identity="alice.rig1",
                blockhash="block-1",
                source="translator_log",
                reward_sats=50_000_000,
                reward_fetched_at=datetime(2026, 1, 1, 0, 6, 0),
                settlement_id=settlement.id,
            )
        )
        session.commit()

    return {
        "db_file": db_file,
        "session_factory": Session,
        "settlement_id": 1,
        "work_window_start": datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        "work_window_end": datetime(2026, 1, 1, 0, 10, 0, tzinfo=UTC),
        "settlement_run_at": datetime(2026, 1, 1, 0, 10, 0, tzinfo=UTC),
    }


def test_shadow_compare_returns_clear_error_when_postgres_not_configured(monkeypatch, tmp_path) -> None:
    seeded = _seed_sqlite_settlement(tmp_path)
    monkeypatch.setenv("DB_PATH", str(seeded["db_file"]))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("POSTGRES_LEDGER_DATABASE_URL", raising=False)

    client = TestClient(app)
    response = client.get(f"/postgres-shadow/settlements/{seeded['settlement_id']}/compare")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["comparison_status"] == "error"
    assert "not configured" in payload["error"]


def test_shadow_compare_returns_not_found_when_shadow_rows_are_missing(monkeypatch, tmp_path) -> None:
    seeded = _seed_sqlite_settlement(tmp_path)
    repository = FakeCompareRepository(
        expected_work_window_start=seeded["work_window_start"],
        expected_work_window_end=seeded["work_window_end"],
        settlement_row=None,
    )

    monkeypatch.setenv("DB_PATH", str(seeded["db_file"]))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(
        "app.postgres_shadow_compare.get_postgres_shadow_compare_repository",
        lambda: repository,
    )

    client = TestClient(app)
    response = client.get(f"/postgres-shadow/settlements/{seeded['settlement_id']}/compare")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["comparison_status"] == "not_found"
    assert payload["postgres_summary"] is None
    assert payload["mismatches"] == []


def test_shadow_compare_returns_matched_when_sqlite_and_postgres_align(monkeypatch, tmp_path) -> None:
    seeded = _seed_sqlite_settlement(tmp_path)
    repository = FakeCompareRepository(
        expected_work_window_start=seeded["work_window_start"],
        expected_work_window_end=seeded["work_window_end"],
        settlement_row={
            "id": 41,
            "status": "completed",
            "settlement_run_at": seeded["settlement_run_at"],
            "work_window_start": seeded["work_window_start"],
            "work_window_end": seeded["work_window_end"],
            "total_reward_sats": 50_000_000,
            "total_work": Decimal("100.0000000000000000"),
            "total_shares": 5,
        },
        credit_rows=[
            {
                "id": 9,
                "settlement_id": 41,
                "user_id": 1,
                "username": "alice",
                "amount_sats": 50_000_000,
                "idempotency_key": "settlement-1-user-1",
                "status": "pending",
                "created_at": seeded["settlement_run_at"],
            }
        ],
        block_rows=[
            {
                "id": 3,
                "settlement_id": 41,
                "blockhash": "block-1",
                "reward_sats": 50_000_000,
                "found_at": datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC),
                "channel_id": 7,
                "worker_identity": "alice.rig1",
                "source": "translator_log",
            }
        ],
    )

    monkeypatch.setenv("DB_PATH", str(seeded["db_file"]))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(
        "app.postgres_shadow_compare.get_postgres_shadow_compare_repository",
        lambda: repository,
    )

    client = TestClient(app)
    response = client.get(f"/v1/postgres-shadow/settlements/{seeded['settlement_id']}/compare")

    assert response.status_code == 200
    payload = response.json()
    assert payload["comparison_status"] == "matched"
    assert payload["mismatches"] == []
    assert payload["sqlite_summary"]["total_reward_sats"] == 50_000_000
    assert payload["postgres_summary"]["settlement_window_id"] == 41


def test_shadow_compare_returns_mismatched_with_clear_details(monkeypatch, tmp_path) -> None:
    seeded = _seed_sqlite_settlement(tmp_path)
    repository = FakeCompareRepository(
        expected_work_window_start=seeded["work_window_start"],
        expected_work_window_end=seeded["work_window_end"],
        settlement_row={
            "id": 51,
            "status": "completed",
            "settlement_run_at": seeded["settlement_run_at"],
            "work_window_start": seeded["work_window_start"],
            "work_window_end": seeded["work_window_end"],
            "total_reward_sats": 49_000_000,
            "total_work": Decimal("100.0000000000000000"),
            "total_shares": 5,
        },
        credit_rows=[
            {
                "id": 10,
                "settlement_id": 51,
                "user_id": 1,
                "username": "alice",
                "amount_sats": 49_000_000,
                "idempotency_key": "settlement-1-user-1",
                "status": "pending",
                "created_at": seeded["settlement_run_at"],
            }
        ],
        block_rows=[
            {
                "id": 4,
                "settlement_id": 51,
                "blockhash": "block-1",
                "reward_sats": 49_000_000,
                "found_at": datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC),
                "channel_id": 7,
                "worker_identity": "alice.rig1",
                "source": "translator_log",
            }
        ],
    )

    monkeypatch.setenv("DB_PATH", str(seeded["db_file"]))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(
        "app.postgres_shadow_compare.get_postgres_shadow_compare_repository",
        lambda: repository,
    )

    client = TestClient(app)
    response = client.get(f"/postgres-shadow/settlements/{seeded['settlement_id']}/compare")

    assert response.status_code == 200
    payload = response.json()
    assert payload["comparison_status"] == "mismatched"
    mismatch_fields = {row["field"] for row in payload["mismatches"]}
    assert "total_reward_sats" in mismatch_fields
    assert "rewarded_block_total_sats" in mismatch_fields
    assert "user_payouts.alice" in mismatch_fields


def test_shadow_compare_is_read_only(monkeypatch, tmp_path) -> None:
    seeded = _seed_sqlite_settlement(tmp_path)
    repository = ReadOnlyGuardRepository(
        expected_work_window_start=seeded["work_window_start"],
        expected_work_window_end=seeded["work_window_end"],
        settlement_row={
            "id": 61,
            "status": "completed",
            "settlement_run_at": seeded["settlement_run_at"],
            "work_window_start": seeded["work_window_start"],
            "work_window_end": seeded["work_window_end"],
            "total_reward_sats": 50_000_000,
            "total_work": Decimal("100.0000000000000000"),
            "total_shares": 5,
        },
        credit_rows=[
            {
                "id": 11,
                "settlement_id": 61,
                "user_id": 1,
                "username": "alice",
                "amount_sats": 50_000_000,
                "idempotency_key": "settlement-1-user-1",
                "status": "pending",
                "created_at": seeded["settlement_run_at"],
            }
        ],
        block_rows=[
            {
                "id": 5,
                "settlement_id": 61,
                "blockhash": "block-1",
                "reward_sats": 50_000_000,
                "found_at": datetime(2026, 1, 1, 0, 5, 0, tzinfo=UTC),
                "channel_id": 7,
                "worker_identity": "alice.rig1",
                "source": "translator_log",
            }
        ],
    )

    monkeypatch.setattr(
        "app.postgres_shadow_compare.get_postgres_shadow_compare_repository",
        lambda: repository,
    )

    with seeded["session_factory"]() as session:
        before_counts = (
            session.query(Settlement).count(),
            session.query(UserPayout).count(),
            session.query(SnapshotBlock).count(),
        )
        payload, status_code = compare_postgres_shadow_settlement(session, seeded["settlement_id"])
        after_counts = (
            session.query(Settlement).count(),
            session.query(UserPayout).count(),
            session.query(SnapshotBlock).count(),
        )

    assert status_code == 200
    assert payload["comparison_status"] == "matched"
    assert before_counts == after_counts
    assert repository.read_calls == [
        "get_settlement_window_by_range",
        "list_settlement_user_credits_with_users:61",
        "list_settlement_blocks:61",
    ]
