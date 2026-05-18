from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from app.db import Base, make_engine, make_session_factory
from app.main import app
from app.models import Settlement, SnapshotBlock, User, UserPayout
from app.postgres_shadow_compare import audit_postgres_shadow_settlements


class BulkFakeCompareRepository:
    def __init__(
        self,
        *,
        settlement_rows_by_window: dict[tuple[datetime, datetime], dict[str, object] | None],
        credits_by_settlement_id: dict[int, list[dict[str, object]]] | None = None,
        blocks_by_settlement_id: dict[int, list[dict[str, object]]] | None = None,
    ) -> None:
        self.settlement_rows_by_window = settlement_rows_by_window
        self.credits_by_settlement_id = credits_by_settlement_id or {}
        self.blocks_by_settlement_id = blocks_by_settlement_id or {}
        self.read_calls: list[str] = []

    def get_settlement_window_by_range(
        self,
        *,
        work_window_start: datetime,
        work_window_end: datetime,
    ) -> dict[str, object] | None:
        self.read_calls.append(
            f"get_settlement_window_by_range:{work_window_start.isoformat()}:{work_window_end.isoformat()}"
        )
        return self.settlement_rows_by_window.get((work_window_start, work_window_end))

    def list_settlement_user_credits_with_users(self, settlement_id: int) -> list[dict[str, object]]:
        self.read_calls.append(f"list_settlement_user_credits_with_users:{settlement_id}")
        return list(self.credits_by_settlement_id.get(settlement_id, []))

    def list_settlement_blocks(self, settlement_id: int) -> list[dict[str, object]]:
        self.read_calls.append(f"list_settlement_blocks:{settlement_id}")
        return list(self.blocks_by_settlement_id.get(settlement_id, []))


class ReadOnlyBulkRepository(BulkFakeCompareRepository):
    def __getattr__(self, name: str):
        if name.startswith(("upsert_", "create_", "set_", "link_")):
            raise AssertionError(f"Unexpected write call attempted: {name}")
        raise AttributeError(name)


def _seed_bulk_sqlite_settlements(tmp_path):
    db_file = tmp_path / "shadow_bulk.db"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)

    created: list[dict[str, object]] = []
    with Session() as session:
        user = User(username="alice")
        session.add(user)
        session.flush()

        specs = [
            (datetime(2026, 1, 1, 0, 0, 0), Decimal("0.50000000")),
            (datetime(2026, 1, 1, 0, 10, 0), Decimal("0.25000000")),
            (datetime(2026, 1, 1, 0, 20, 0), Decimal("0.75000000")),
        ]
        for idx, (period_start, reward_btc) in enumerate(specs, start=1):
            period_end = period_start + timedelta(minutes=10)
            settlement = Settlement(
                status="completed",
                period_start=period_start,
                period_end=period_end,
                total_shares=idx * 5,
                total_work=Decimal(str(idx * 100)),
                pool_reward_btc=reward_btc,
            )
            session.add(settlement)
            session.flush()

            session.add(
                UserPayout(
                    settlement_id=settlement.id,
                    user_id=user.id,
                    contribution_value=Decimal(str(idx * 100)),
                    payout_fraction=Decimal("1.000000000000"),
                    amount_btc=reward_btc,
                    idempotency_key=f"settlement-{settlement.id}-user-{user.id}",
                    status="pending",
                )
            )
            reward_sats = int(reward_btc * Decimal("100000000"))
            session.add(
                SnapshotBlock(
                    found_at=period_start + timedelta(minutes=5),
                    channel_id=idx,
                    worker_identity="alice.rig1",
                    blockhash=f"block-{idx}",
                    source="translator_log",
                    reward_sats=reward_sats,
                    reward_fetched_at=period_start + timedelta(minutes=6),
                    settlement_id=settlement.id,
                )
            )
            created.append(
                {
                    "sqlite_settlement_id": settlement.id,
                    "period_start": period_start,
                    "period_end": period_end,
                    "work_window_start": period_start.replace(tzinfo=UTC),
                    "work_window_end": period_end.replace(tzinfo=UTC),
                    "settlement_run_at": period_end.replace(tzinfo=UTC),
                    "reward_sats": reward_sats,
                    "total_shares": idx * 5,
                    "total_work": Decimal(str(idx * 100)),
                    "blockhash": f"block-{idx}",
                }
            )

        session.commit()

    return {
        "db_file": db_file,
        "session_factory": Session,
        "settlements": created,
    }


def _matched_repo_payload(seeded: dict[str, object]) -> BulkFakeCompareRepository:
    settlement_rows_by_window: dict[tuple[datetime, datetime], dict[str, object] | None] = {}
    credits_by_settlement_id: dict[int, list[dict[str, object]]] = {}
    blocks_by_settlement_id: dict[int, list[dict[str, object]]] = {}

    for idx, settlement in enumerate(seeded["settlements"], start=101):
        window_key = (settlement["work_window_start"], settlement["work_window_end"])
        settlement_rows_by_window[window_key] = {
            "id": idx,
            "status": "completed",
            "settlement_run_at": settlement["settlement_run_at"],
            "work_window_start": settlement["work_window_start"],
            "work_window_end": settlement["work_window_end"],
            "total_reward_sats": settlement["reward_sats"],
            "total_work": Decimal(str(settlement["total_work"])) * Decimal("1.0000000000000000"),
            "total_shares": settlement["total_shares"],
        }
        credits_by_settlement_id[idx] = [
            {
                "id": idx * 10,
                "settlement_id": idx,
                "user_id": 1,
                "username": "alice",
                "amount_sats": settlement["reward_sats"],
                "idempotency_key": f"settlement-{settlement['sqlite_settlement_id']}-user-1",
                "status": "pending",
                "created_at": settlement["settlement_run_at"],
            }
        ]
        blocks_by_settlement_id[idx] = [
            {
                "id": idx * 20,
                "settlement_id": idx,
                "blockhash": settlement["blockhash"],
                "reward_sats": settlement["reward_sats"],
                "found_at": settlement["work_window_start"] + timedelta(minutes=5),
                "channel_id": 1,
                "worker_identity": "alice.rig1",
                "source": "translator_log",
            }
        ]

    return BulkFakeCompareRepository(
        settlement_rows_by_window=settlement_rows_by_window,
        credits_by_settlement_id=credits_by_settlement_id,
        blocks_by_settlement_id=blocks_by_settlement_id,
    )


def test_shadow_bulk_audit_returns_clear_error_when_postgres_not_configured(monkeypatch, tmp_path) -> None:
    seeded = _seed_bulk_sqlite_settlements(tmp_path)
    monkeypatch.setenv("DB_PATH", str(seeded["db_file"]))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("POSTGRES_LEDGER_DATABASE_URL", raising=False)

    client = TestClient(app)
    response = client.get("/postgres-shadow/settlements/audit")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["comparison_status"] == "error"
    assert "not configured" in payload["error"]


def test_shadow_bulk_audit_returns_matched_summary(monkeypatch, tmp_path) -> None:
    seeded = _seed_bulk_sqlite_settlements(tmp_path)
    repository = _matched_repo_payload(seeded)

    monkeypatch.setenv("DB_PATH", str(seeded["db_file"]))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(
        "app.postgres_shadow_compare.get_postgres_shadow_compare_repository",
        lambda: repository,
    )

    client = TestClient(app)
    response = client.get("/v1/postgres-shadow/settlements/audit?limit=10")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["comparison_status"] == "matched"
    assert payload["total_checked"] == 3
    assert payload["matched_count"] == 3
    assert payload["mismatched_count"] == 0
    assert payload["not_found_count"] == 0
    assert payload["error_count"] == 0
    assert [row["settlement_id"] for row in payload["rows"]] == [3, 2, 1]


def test_shadow_bulk_audit_counts_missing_postgres_rows(monkeypatch, tmp_path) -> None:
    seeded = _seed_bulk_sqlite_settlements(tmp_path)
    repository = BulkFakeCompareRepository(settlement_rows_by_window={})

    monkeypatch.setenv("DB_PATH", str(seeded["db_file"]))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(
        "app.postgres_shadow_compare.get_postgres_shadow_compare_repository",
        lambda: repository,
    )

    client = TestClient(app)
    response = client.get("/postgres-shadow/settlements/audit")

    assert response.status_code == 200
    payload = response.json()
    assert payload["comparison_status"] == "not_found"
    assert payload["total_checked"] == 3
    assert payload["not_found_count"] == 3
    assert all(row["comparison_status"] == "not_found" for row in payload["rows"])


def test_shadow_bulk_audit_counts_mismatches(monkeypatch, tmp_path) -> None:
    seeded = _seed_bulk_sqlite_settlements(tmp_path)
    repository = _matched_repo_payload(seeded)
    newest = seeded["settlements"][-1]
    mismatched_window = (newest["work_window_start"], newest["work_window_end"])
    mismatched_settlement_id = int(repository.settlement_rows_by_window[mismatched_window]["id"])
    repository.settlement_rows_by_window[mismatched_window]["total_reward_sats"] = newest["reward_sats"] - 1
    repository.credits_by_settlement_id[mismatched_settlement_id][0]["amount_sats"] = newest["reward_sats"] - 1
    repository.blocks_by_settlement_id[mismatched_settlement_id][0]["reward_sats"] = newest["reward_sats"] - 1

    monkeypatch.setenv("DB_PATH", str(seeded["db_file"]))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(
        "app.postgres_shadow_compare.get_postgres_shadow_compare_repository",
        lambda: repository,
    )

    client = TestClient(app)
    response = client.get("/postgres-shadow/settlements/audit")

    assert response.status_code == 200
    payload = response.json()
    assert payload["comparison_status"] == "mismatched"
    assert payload["matched_count"] == 2
    assert payload["mismatched_count"] == 1
    mismatch_row = next(row for row in payload["rows"] if row["comparison_status"] == "mismatched")
    assert mismatch_row["mismatch_count"] >= 3


def test_shadow_bulk_audit_status_filter_returns_only_requested_rows(monkeypatch, tmp_path) -> None:
    seeded = _seed_bulk_sqlite_settlements(tmp_path)
    repository = _matched_repo_payload(seeded)
    newest = seeded["settlements"][-1]
    middle = seeded["settlements"][-2]
    newest_key = (newest["work_window_start"], newest["work_window_end"])
    middle_key = (middle["work_window_start"], middle["work_window_end"])
    newest_shadow_id = int(repository.settlement_rows_by_window[newest_key]["id"])
    repository.settlement_rows_by_window[newest_key]["total_reward_sats"] = newest["reward_sats"] - 1
    repository.credits_by_settlement_id[newest_shadow_id][0]["amount_sats"] = newest["reward_sats"] - 1
    repository.blocks_by_settlement_id[newest_shadow_id][0]["reward_sats"] = newest["reward_sats"] - 1
    repository.settlement_rows_by_window[middle_key] = None

    monkeypatch.setenv("DB_PATH", str(seeded["db_file"]))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(
        "app.postgres_shadow_compare.get_postgres_shadow_compare_repository",
        lambda: repository,
    )

    client = TestClient(app)
    response = client.get("/postgres-shadow/settlements/audit?status_filter=mismatched")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total_checked"] == 3
    assert payload["matched_count"] == 1
    assert payload["mismatched_count"] == 1
    assert payload["not_found_count"] == 1
    assert len(payload["rows"]) == 1
    assert payload["rows"][0]["comparison_status"] == "mismatched"


def test_shadow_bulk_audit_include_details_false_omits_mismatch_arrays(monkeypatch, tmp_path) -> None:
    seeded = _seed_bulk_sqlite_settlements(tmp_path)
    repository = _matched_repo_payload(seeded)
    newest = seeded["settlements"][-1]
    newest_key = (newest["work_window_start"], newest["work_window_end"])
    newest_shadow_id = int(repository.settlement_rows_by_window[newest_key]["id"])
    repository.settlement_rows_by_window[newest_key]["total_reward_sats"] = newest["reward_sats"] - 1
    repository.credits_by_settlement_id[newest_shadow_id][0]["amount_sats"] = newest["reward_sats"] - 1
    repository.blocks_by_settlement_id[newest_shadow_id][0]["reward_sats"] = newest["reward_sats"] - 1

    monkeypatch.setenv("DB_PATH", str(seeded["db_file"]))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(
        "app.postgres_shadow_compare.get_postgres_shadow_compare_repository",
        lambda: repository,
    )

    client = TestClient(app)
    response = client.get("/postgres-shadow/settlements/audit")

    assert response.status_code == 200
    mismatch_row = next(row for row in response.json()["rows"] if row["comparison_status"] == "mismatched")
    assert "mismatches" not in mismatch_row


def test_shadow_bulk_audit_include_details_true_includes_mismatch_arrays(monkeypatch, tmp_path) -> None:
    seeded = _seed_bulk_sqlite_settlements(tmp_path)
    repository = _matched_repo_payload(seeded)
    newest = seeded["settlements"][-1]
    newest_key = (newest["work_window_start"], newest["work_window_end"])
    newest_shadow_id = int(repository.settlement_rows_by_window[newest_key]["id"])
    repository.settlement_rows_by_window[newest_key]["total_reward_sats"] = newest["reward_sats"] - 1
    repository.credits_by_settlement_id[newest_shadow_id][0]["amount_sats"] = newest["reward_sats"] - 1
    repository.blocks_by_settlement_id[newest_shadow_id][0]["reward_sats"] = newest["reward_sats"] - 1

    monkeypatch.setenv("DB_PATH", str(seeded["db_file"]))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(
        "app.postgres_shadow_compare.get_postgres_shadow_compare_repository",
        lambda: repository,
    )

    client = TestClient(app)
    response = client.get("/postgres-shadow/settlements/audit?include_details=true")

    assert response.status_code == 200
    mismatch_row = next(row for row in response.json()["rows"] if row["comparison_status"] == "mismatched")
    assert isinstance(mismatch_row["mismatches"], list)
    assert len(mismatch_row["mismatches"]) >= 3


def test_shadow_bulk_audit_is_read_only(monkeypatch, tmp_path) -> None:
    seeded = _seed_bulk_sqlite_settlements(tmp_path)
    base_repository = _matched_repo_payload(seeded)
    repository = ReadOnlyBulkRepository(
        settlement_rows_by_window=base_repository.settlement_rows_by_window,
        credits_by_settlement_id=base_repository.credits_by_settlement_id,
        blocks_by_settlement_id=base_repository.blocks_by_settlement_id,
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
        payload, status_code = audit_postgres_shadow_settlements(session, include_details=True)
        after_counts = (
            session.query(Settlement).count(),
            session.query(UserPayout).count(),
            session.query(SnapshotBlock).count(),
        )

    assert status_code == 200
    assert payload["comparison_status"] == "matched"
    assert before_counts == after_counts
