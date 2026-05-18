from datetime import UTC, datetime
from decimal import Decimal
import json

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.db import Base, make_engine, make_session_factory
from app.main import app
from app.models import Settlement, User, UserPayout


POSTGRES_READ_ENV_KEYS = [
    "POSTGRES_LEDGER_READS_ENABLED",
    "POSTGRES_LEDGER_READ_FALLBACK_TO_SQLITE",
    "POSTGRES_LEDGER_READ_REQUIRE_SHADOW_MATCH",
    "POSTGRES_LEDGER_READ_ALLOWED_ENDPOINTS",
    "POSTGRES_LEDGER_READ_MODE",
    "POSTGRES_LEDGER_DATABASE_URL",
]


class FakePostgresReadRepository:
    def __init__(self, rows=None, exc: Exception | None = None) -> None:
        self.rows = rows or []
        self.exc = exc
        self.calls = 0

    def list_settlement_history(self, limit: int = 100):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return list(self.rows[:limit])


def _clear_postgres_read_env(monkeypatch) -> None:
    for key in POSTGRES_READ_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _seed_sqlite(tmp_path, monkeypatch):
    db_file = tmp_path / "read_candidate_bundle.db"
    audit_log = tmp_path / "payout_audit.jsonl"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)

    with Session() as session:
        user = User(username="sqlite-user")
        session.add(user)
        session.flush()
        settlement = Settlement(
            status="completed",
            period_start=datetime(2026, 1, 1, 1, 0, 0),
            period_end=datetime(2026, 1, 1, 1, 10, 0),
            total_shares=12,
            total_work=Decimal("120.00000000"),
            pool_reward_btc=Decimal("0.12000000"),
        )
        session.add(settlement)
        session.flush()
        session.add(
            UserPayout(
                settlement_id=settlement.id,
                user_id=user.id,
                contribution_value=Decimal("120.00000000"),
                payout_fraction=Decimal("1.000000000000"),
                amount_btc=Decimal("0.12000000"),
                idempotency_key=f"settlement-{settlement.id}-user-{user.id}",
                status="pending",
            )
        )
        session.commit()

    audit_entry = {
        "attempt_id": "sqlite-attempt",
        "attempted_at": "2026-01-01T01:10:00",
        "period_start": "2026-01-01T01:00:00",
        "period_end": "2026-01-01T01:10:00",
        "settlement": {
            "settlement_id": 1,
            "status": "completed",
            "reward_mode": "blocks",
            "pool_reward_btc": "0.12000000",
            "total_work": "120.00000000",
            "total_shares": 12,
        },
        "payout_rows": [
            {
                "username": "sqlite-user",
                "amount_btc": "0.12000000",
                "status": "pending",
                "payout_fraction": "1.000000000000",
                "contribution_value": "120.00000000",
            }
        ],
        "checks": {"unrewarded_user_count": 0, "unrewarded_users": []},
        "block_reward": {"interval_blocks": 0, "computed_reward_btc": "0.00000000"},
        "snapshot_alignment": {"total_share_delta": 12, "total_work_delta": "120.00000000"},
    }
    audit_log.write_text(json.dumps(audit_entry) + "\n", encoding="utf-8")

    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(audit_log))
    return {"db_file": db_file, "audit_log": audit_log}


def _postgres_rows():
    return [
        {
            "id": 900,
            "status": "completed",
            "settlement_run_at": datetime(2026, 1, 2, 1, 10, 0, tzinfo=UTC),
            "work_window_start": datetime(2026, 1, 2, 1, 0, 0, tzinfo=UTC),
            "work_window_end": datetime(2026, 1, 2, 1, 10, 0, tzinfo=UTC),
            "total_reward_sats": 12_000_000,
            "total_work": Decimal("120.0000000000000000"),
            "total_shares": 12,
            "user_credits": [
                {
                    "id": 91,
                    "settlement_id": 900,
                    "user_id": 9,
                    "username": "postgres-user",
                    "amount_sats": 12_000_000,
                    "idempotency_key": "settlement-900-user-9",
                    "status": "pending",
                    "created_at": datetime(2026, 1, 2, 1, 10, 0, tzinfo=UTC),
                }
            ],
            "user_work": [
                {
                    "id": 92,
                    "settlement_id": 900,
                    "user_id": 9,
                    "username": "postgres-user",
                    "share_delta": 12,
                    "work_delta": Decimal("120.0000000000000000"),
                    "payout_fraction": Decimal("1.000000000000000000"),
                }
            ],
            "settlement_blocks": [],
        }
    ]


def _postgres_rows_with_mapping(sqlite_settlement_id: int = 501):
    rows = _postgres_rows()
    rows[0]["sqlite_settlement_id"] = sqlite_settlement_id
    return rows


def _allow_candidate(monkeypatch, endpoints: str, *, require_match: bool = True) -> None:
    monkeypatch.setenv("POSTGRES_LEDGER_READS_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_LEDGER_READ_MODE", "postgres_shadow_candidate")
    monkeypatch.setenv("POSTGRES_LEDGER_READ_ALLOWED_ENDPOINTS", endpoints)
    monkeypatch.setenv("POSTGRES_LEDGER_READ_REQUIRE_SHADOW_MATCH", "true" if require_match else "false")


def _mock_shadow_audit(monkeypatch, comparison_status: str = "matched") -> None:
    def _audit(*args, **kwargs):
        mismatched = 1 if comparison_status == "mismatched" else 0
        return (
            {
                "comparison_status": comparison_status,
                "matched_count": 1 if comparison_status == "matched" else 0,
                "mismatched_count": mismatched,
                "not_found_count": 0,
                "error_count": 0,
            },
            200,
        )

    monkeypatch.setattr("app.main.audit_postgres_shadow_settlements", _audit)


@pytest.mark.parametrize(
    ("path", "sqlite_marker"),
    [
        ("/audit/settlements?limit=5", "settlements"),
        ("/settlements/latest", "users"),
    ],
)
def test_default_settings_use_sqlite_for_candidate_endpoints(monkeypatch, tmp_path, path, sqlite_marker) -> None:
    _clear_postgres_read_env(monkeypatch)
    _seed_sqlite(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.main._get_postgres_candidate_read_repository",
        lambda: pytest.fail("Postgres repository should not be used by default"),
    )
    monkeypatch.setattr(
        "app.main.audit_postgres_shadow_settlements",
        lambda *args, **kwargs: pytest.fail("Shadow audit gate should not run by default"),
    )

    response = TestClient(app).get(path)

    assert response.status_code == 200
    payload = response.json()
    assert "read_diagnostics" not in payload
    if sqlite_marker == "settlements":
        assert payload["settlements"][0]["settlement_id"] == 1
    else:
        assert payload["users"][0]["username"] == "sqlite-user"


def test_reads_enabled_but_endpoint_not_allowed_uses_sqlite(monkeypatch, tmp_path) -> None:
    _clear_postgres_read_env(monkeypatch)
    _seed_sqlite(tmp_path, monkeypatch)
    _allow_candidate(monkeypatch, "settlement_detail")
    monkeypatch.setattr(
        "app.main._get_postgres_candidate_read_repository",
        lambda: pytest.fail("Postgres repository should not be used for a disallowed endpoint"),
    )
    monkeypatch.setattr(
        "app.main.audit_postgres_shadow_settlements",
        lambda *args, **kwargs: pytest.fail("Shadow audit should not run before endpoint allow-list passes"),
    )

    response = TestClient(app).get("/audit/settlements?limit=5")

    assert response.status_code == 200
    assert response.json()["settlements"][0]["settlement_id"] == 1


@pytest.mark.parametrize(
    ("path", "endpoint_id"),
    [
        ("/audit/settlements?limit=5", "settlement_history"),
        ("/settlements/latest", "settlement_detail"),
    ],
)
def test_reads_enabled_allowed_candidate_mode_uses_sqlite_when_settlement_id_mapping_is_unavailable(
    monkeypatch,
    tmp_path,
    path,
    endpoint_id,
) -> None:
    _clear_postgres_read_env(monkeypatch)
    _seed_sqlite(tmp_path, monkeypatch)
    _allow_candidate(monkeypatch, endpoint_id)
    monkeypatch.setattr(
        "app.main.audit_postgres_shadow_settlements",
        lambda *args, **kwargs: pytest.fail("Shadow audit should not run when source settlement id mapping is missing"),
    )
    monkeypatch.setattr(
        "app.main._get_postgres_candidate_read_repository",
        lambda: FakePostgresReadRepository(rows=_postgres_rows()),
    )

    response = TestClient(app).get(path)

    assert response.status_code == 200
    payload = response.json()
    assert "read_diagnostics" not in payload
    if endpoint_id == "settlement_history":
        assert payload["settlements"][0]["settlement_id"] == 1
        assert payload["settlements"][0]["payout_user_breakdown"][0]["username"] == "sqlite-user"
    else:
        assert payload["settlement"]["settlement_id"] == 1
        assert payload["users"][0]["username"] == "sqlite-user"


def test_require_shadow_match_blocks_postgres_when_audit_is_not_matched(monkeypatch, tmp_path) -> None:
    _clear_postgres_read_env(monkeypatch)
    _seed_sqlite(tmp_path, monkeypatch)
    _allow_candidate(monkeypatch, "settlement_detail")
    _mock_shadow_audit(monkeypatch, "mismatched")
    monkeypatch.setattr(
        "app.main._get_postgres_candidate_read_repository",
        lambda: FakePostgresReadRepository(rows=_postgres_rows_with_mapping(600)),
    )

    response = TestClient(app).get("/settlements/latest")

    assert response.status_code == 200
    payload = response.json()
    assert "read_diagnostics" not in payload
    assert payload["users"][0]["username"] == "sqlite-user"


def test_postgres_surrogate_id_is_never_exposed_from_history_helper() -> None:
    rows = _postgres_rows()

    with pytest.raises(RuntimeError, match="public SQLite settlement id mapping"):
        main_module._normalize_postgres_settlement_history_rows(rows)


def test_postgres_surrogate_id_is_never_exposed_from_latest_helper(monkeypatch) -> None:
    monkeypatch.setattr("app.main._postgres_settlement_history_rows", lambda limit: _postgres_rows())

    with pytest.raises(RuntimeError, match="public SQLite settlement id mapping"):
        main_module._read_postgres_latest_settlement()


def test_candidate_read_with_mapping_unavailable_uses_sqlite_and_does_not_expose_secrets(
    monkeypatch,
    tmp_path,
) -> None:
    _clear_postgres_read_env(monkeypatch)
    _seed_sqlite(tmp_path, monkeypatch)
    _allow_candidate(monkeypatch, "settlement_history", require_match=False)
    monkeypatch.setenv("POSTGRES_LEDGER_READ_FALLBACK_TO_SQLITE", "true")
    monkeypatch.setattr(
        "app.main._get_postgres_candidate_read_repository",
        lambda: FakePostgresReadRepository(exc=RuntimeError("postgres://user:secret@localhost/db")),
    )

    response = TestClient(app).get("/audit/settlements?limit=5")

    assert response.status_code == 200
    assert response.json()["settlements"][0]["settlement_id"] == 1
    assert "read_diagnostics" not in response.json()
    assert "secret" not in response.text
    assert "postgres://" not in response.text


def test_mapping_unavailable_keeps_sqlite_even_when_fallback_disabled(monkeypatch, tmp_path) -> None:
    _clear_postgres_read_env(monkeypatch)
    _seed_sqlite(tmp_path, monkeypatch)
    _allow_candidate(monkeypatch, "settlement_detail", require_match=False)
    monkeypatch.setenv("POSTGRES_LEDGER_READ_FALLBACK_TO_SQLITE", "false")
    monkeypatch.setenv("POSTGRES_LEDGER_DATABASE_URL", "postgres://user:supersecret@localhost/db")
    monkeypatch.setattr(
        "app.main._get_postgres_candidate_read_repository",
        lambda: FakePostgresReadRepository(exc=RuntimeError("supersecret")),
    )

    response = TestClient(app).get("/settlements/latest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["settlement"]["settlement_id"] == 1
    assert payload["users"][0]["username"] == "sqlite-user"
    assert "read_diagnostics" not in payload
    assert "supersecret" not in response.text
    assert "postgres://" not in response.text


def test_candidate_history_uses_postgres_when_mapping_exists(monkeypatch, tmp_path) -> None:
    _clear_postgres_read_env(monkeypatch)
    _seed_sqlite(tmp_path, monkeypatch)
    _allow_candidate(monkeypatch, "settlement_history", require_match=False)

    monkeypatch.setattr(
        "app.main._get_postgres_candidate_read_repository",
        lambda: FakePostgresReadRepository(rows=_postgres_rows_with_mapping(777)),
    )

    response = TestClient(app).get("/audit/settlements?limit=5")

    assert response.status_code == 200
    payload = response.json()
    assert payload["settlements"][0]["settlement_id"] == 777
    assert payload["settlements"][0]["raw"]["read_source"] == "postgres"
    assert payload["settlements"][0]["raw"]["settlement_window_id"] == 900
    assert payload["read_diagnostics"]["read_source"] == "postgres"
    assert payload["read_diagnostics"]["postgres_read_endpoint"] == "settlement_history"


def test_candidate_detail_uses_postgres_when_mapping_exists(monkeypatch, tmp_path) -> None:
    _clear_postgres_read_env(monkeypatch)
    _seed_sqlite(tmp_path, monkeypatch)
    _allow_candidate(monkeypatch, "settlement_detail", require_match=False)

    monkeypatch.setattr(
        "app.main._get_postgres_candidate_read_repository",
        lambda: FakePostgresReadRepository(rows=_postgres_rows_with_mapping(778)),
    )

    response = TestClient(app).get("/settlements/latest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["settlement"]["settlement_id"] == 778
    assert payload["users"][0]["username"] == "postgres-user"
    assert payload["read_diagnostics"]["read_source"] == "postgres"
    assert payload["read_diagnostics"]["postgres_read_endpoint"] == "settlement_detail"
