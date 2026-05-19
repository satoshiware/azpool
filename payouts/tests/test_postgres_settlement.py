import pytest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.db import Base, make_engine, make_session_factory
from app.models import User, Settlement, UserPayout, CarryState, WorkAccrualBucket
from app.postgres_repositories import PostgresLedgerRepository
from app.postgres_settlement import (
    run_settlement_postgres,
    _get_or_create_carry_postgres,
    _get_or_create_accrual_bucket_postgres,
)
from app.pool_client import PoolApiError
from app.settlement import run_settlement, SettlementResult
from app.delta import UserContribution


@pytest.fixture
def sqlite_session(tmp_path: Path):
    """Create SQLite session for testing."""
    db_file = tmp_path / "settlement_test.db"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    SessionFactory = make_session_factory(engine)
    with SessionFactory() as s:
        yield s


@pytest.fixture
def postgres_session_factory(tmp_path: Path):
    """Create PostgreSQL session factory for testing (mock via SQLite for CI)."""
    db_file = tmp_path / "postgres_settlement_test.db"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@pytest.fixture
def setup_carry_state(postgres_session_factory):
    """Initialize carry state to zero."""
    repository = PostgresLedgerRepository(postgres_session_factory)
    repository.upsert_carry_state(
        bucket="default",
        carry_btc=Decimal("0"),
        updated_at=datetime.now(UTC),
    )
    return repository


class TestPostgresCarryState:
    """Tests for carry state management in Postgres."""
    
    def test_get_or_create_carry_state_new(self, setup_carry_state):
        """Create new carry state when it doesn't exist."""
        repo = setup_carry_state
        
        # Verify initial state
        carry = repo.get_carry_state(bucket="default")
        assert carry is not None
        assert Decimal(str(carry["carry_btc"])) == Decimal("0")
    
    def test_upsert_carry_state_update(self, setup_carry_state):
        """Update existing carry state."""
        repo = setup_carry_state
        
        # Update carry
        repo.upsert_carry_state(
            bucket="default",
            carry_btc=Decimal("0.5"),
            updated_at=datetime.now(UTC),
        )
        
        carry = repo.get_carry_state(bucket="default")
        assert Decimal(str(carry["carry_btc"])) == Decimal("0.5")
    
    def test_carry_state_uniqueness(self, setup_carry_state):
        """Verify bucket uniqueness constraint."""
        repo = setup_carry_state
        
        # Create two carry states with same bucket
        repo.upsert_carry_state(
            bucket="default",
            carry_btc=Decimal("1.0"),
            updated_at=datetime.now(UTC),
        )
        
        repo.upsert_carry_state(
            bucket="default",
            carry_btc=Decimal("2.0"),
            updated_at=datetime.now(UTC),
        )
        
        # Should return only one row
        carry = repo.get_carry_state(bucket="default")
        assert Decimal(str(carry["carry_btc"])) == Decimal("2.0")


class TestPostgresSettlementBasics:
    """Basic sanity tests for settlement module."""

    def test_postgres_settlement_module_imports(self):
        """Verify postgres_settlement module can be imported."""
        from app.postgres_settlement import run_settlement_postgres
        assert callable(run_settlement_postgres)

    def test_next_window_uses_previous_end_as_baseline(self):
        """Ensure consecutive settlements chain fixed windows from prior settlement end."""

        class _FakeRepo:
            def __init__(self, latest_end):
                self.latest_end = latest_end
                self.range_calls = []

            def get_latest_settlement_window(self):
                return {
                    "id": 100,
                    "status": "completed",
                    "work_window_start": self.latest_end - timedelta(minutes=10),
                    "work_window_end": self.latest_end,
                    "total_shares": 10,
                    "total_work": Decimal("100"),
                    "total_reward_sats": 1000,
                    "sqlite_settlement_id": None,
                }

            def get_settlement_window_by_range(self, *, work_window_start, work_window_end):
                self.range_calls.append((work_window_start, work_window_end))
                return {
                    "id": 101,
                    "status": "completed",
                    "work_window_start": work_window_start,
                    "work_window_end": work_window_end,
                    "total_shares": 5,
                    "total_work": Decimal("50"),
                    "total_reward_sats": 500,
                    "sqlite_settlement_id": None,
                }

            def list_settlement_user_credits_with_users(self, _settlement_id):
                return []

            def get_carry_state(self, *, bucket="default"):
                return {"bucket": bucket, "carry_btc": Decimal("0")}

        latest_end = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 12, 15, tzinfo=UTC)
        repo = _FakeRepo(latest_end)

        result = run_settlement_postgres(
            repo,
            now,
            interval_minutes=10,
            payout_decimals=8,
        )

        assert len(repo.range_calls) == 1
        called_start, called_end = repo.range_calls[0]
        assert called_start == latest_end
        assert called_end == latest_end + timedelta(minutes=10)
        assert result.period_start == latest_end
        assert result.period_end == latest_end + timedelta(minutes=10)

    def test_next_window_with_lag_still_starts_from_previous_end(self):
        """Ensure scheduler lag still produces one fixed interval window from prior end."""

        class _FakeRepo:
            def __init__(self, latest_end):
                self.latest_end = latest_end
                self.range_calls = []

            def get_latest_settlement_window(self):
                return {
                    "id": 200,
                    "status": "completed",
                    "work_window_start": self.latest_end - timedelta(minutes=10),
                    "work_window_end": self.latest_end,
                    "total_shares": 10,
                    "total_work": Decimal("100"),
                    "total_reward_sats": 1000,
                    "sqlite_settlement_id": None,
                }

            def get_settlement_window_by_range(self, *, work_window_start, work_window_end):
                self.range_calls.append((work_window_start, work_window_end))
                return {
                    "id": 201,
                    "status": "completed",
                    "work_window_start": work_window_start,
                    "work_window_end": work_window_end,
                    "total_shares": 5,
                    "total_work": Decimal("50"),
                    "total_reward_sats": 500,
                    "sqlite_settlement_id": None,
                }

            def list_settlement_user_credits_with_users(self, _settlement_id):
                return []

            def get_carry_state(self, *, bucket="default"):
                return {"bucket": bucket, "carry_btc": Decimal("0")}

        latest_end = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        # Deliberately later than interval to emulate scheduler drift.
        now = datetime(2026, 1, 1, 12, 12, tzinfo=UTC)
        repo = _FakeRepo(latest_end)

        result = run_settlement_postgres(
            repo,
            now,
            interval_minutes=10,
            payout_decimals=8,
        )

        assert len(repo.range_calls) == 1
        called_start, called_end = repo.range_calls[0]
        assert called_start == latest_end
        assert called_end == latest_end + timedelta(minutes=10)
        assert result.period_start == latest_end
        assert result.period_end == latest_end + timedelta(minutes=10)

    def test_early_scheduler_run_returns_latest_without_creating_partial_window(self):
        """Ensure runs earlier than the full interval do not create short settlement windows."""

        class _FakeRepo:
            def __init__(self, latest_end):
                self.latest_end = latest_end
                self.range_calls = []

            def get_latest_settlement_window(self):
                return {
                    "id": 300,
                    "status": "completed",
                    "work_window_start": self.latest_end - timedelta(minutes=10),
                    "work_window_end": self.latest_end,
                    "total_shares": 10,
                    "total_work": Decimal("100"),
                    "total_reward_sats": 1000,
                    "sqlite_settlement_id": None,
                }

            def get_settlement_window_by_range(self, *, work_window_start, work_window_end):
                self.range_calls.append((work_window_start, work_window_end))
                return None

            def list_settlement_user_credits_with_users(self, _settlement_id):
                return []

            def get_carry_state(self, *, bucket="default"):
                return {"bucket": bucket, "carry_btc": Decimal("0")}

        latest_end = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        now = datetime(2026, 1, 1, 12, 2, tzinfo=UTC)
        repo = _FakeRepo(latest_end)

        result = run_settlement_postgres(
            repo,
            now,
            interval_minutes=10,
            payout_decimals=8,
        )

        assert result.period_start == latest_end - timedelta(minutes=10)
        assert result.period_end == latest_end
        assert not repo.range_calls

    def test_naive_work_window_end_does_not_crash_maturity_offset_math(self):
        """Ensure mixed naive/aware inputs are normalized before maturity offset subtraction."""

        class _FakeRepo:
            def __init__(self):
                self.upsert_calls = []

            def get_latest_settlement_window(self):
                return None

            def get_settlement_window_by_range(self, *, work_window_start, work_window_end):
                _ = (work_window_start, work_window_end)
                return None

            def upsert_settlement_window(self, **kwargs):
                self.upsert_calls.append(kwargs)
                return {"id": 1, **kwargs}

        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        naive_work_window_end = datetime(2026, 1, 1, 11, 50)
        repo = _FakeRepo()

        result = run_settlement_postgres(
            repo,
            now,
            interval_minutes=10,
            payout_decimals=8,
            work_window_end=naive_work_window_end,
            reward_fetcher=lambda _start, _end: (_ for _ in ()).throw(PoolApiError("down")),
        )

        assert result.status == "blocked"
        assert repo.upsert_calls

    def test_deferred_settlement_persists_user_work_rows(self, monkeypatch):
        """Deferred Postgres settlements should still persist per-user work rows."""

        class _FakeRepo:
            def __init__(self):
                self.user_work_calls = []
                self.users = {}

            def get_latest_settlement_window(self):
                return None

            def get_settlement_window_by_range(self, *, work_window_start, work_window_end):
                _ = (work_window_start, work_window_end)
                return None

            def upsert_settlement_window(self, **kwargs):
                return {"id": 1, **kwargs}

            def upsert_user(self, username, created_at=None):
                user = self.users.get(username)
                if user is None:
                    user = {"id": len(self.users) + 1, "username": username, "created_at": created_at}
                    self.users[username] = user
                return user

            def upsert_settlement_user_work(self, **kwargs):
                self.user_work_calls.append(kwargs)
                return kwargs

            def get_carry_state(self, *, bucket="default"):
                return {"bucket": bucket, "carry_btc": Decimal("0")}

        monkeypatch.setattr(
            "app.postgres_settlement.compute_user_contribution_deltas_postgres",
            lambda repo, start, end: {
                "alice": UserContribution("alice", 100, Decimal("1000")),
                "bob": UserContribution("bob", 60, Decimal("1618982")),
            },
        )

        repo = _FakeRepo()
        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

        result = run_settlement_postgres(
            repo,
            now,
            interval_minutes=10,
            payout_decimals=8,
            reward_fetcher=lambda _start, _end: Decimal("0"),
            defer_on_zero_reward=True,
            use_work_accrual=False,
        )

        assert result.status == "deferred"
        assert len(repo.user_work_calls) == 2
        assert {call["share_delta"] for call in repo.user_work_calls} == {100, 60}

    def test_deferred_settlement_uses_update_by_id_after_initial_create(self, monkeypatch):
        """Settlement state transitions should update by id to avoid repeated upsert insert attempts."""

        class _FakeRepo:
            def __init__(self):
                self.upsert_calls = 0
                self.update_calls = 0
                self.user_work_calls = []
                self.users = {}

            def get_latest_settlement_window(self):
                return None

            def get_settlement_window_by_range(self, *, work_window_start, work_window_end):
                _ = (work_window_start, work_window_end)
                return None

            def upsert_settlement_window(self, **kwargs):
                self.upsert_calls += 1
                return {"id": 77, **kwargs}

            def update_settlement_window_by_id(self, **kwargs):
                self.update_calls += 1
                return {"id": kwargs["settlement_id"], **kwargs}

            def upsert_user(self, username, created_at=None):
                user = self.users.get(username)
                if user is None:
                    user = {"id": len(self.users) + 1, "username": username, "created_at": created_at}
                    self.users[username] = user
                return user

            def upsert_settlement_user_work(self, **kwargs):
                self.user_work_calls.append(kwargs)
                return kwargs

            def get_carry_state(self, *, bucket="default"):
                return {"bucket": bucket, "carry_btc": Decimal("0")}

        monkeypatch.setattr(
            "app.postgres_settlement.compute_user_contribution_deltas_postgres",
            lambda repo, start, end: {
                "alice": UserContribution("alice", 100, Decimal("1000")),
                "bob": UserContribution("bob", 60, Decimal("1618982")),
            },
        )

        repo = _FakeRepo()
        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

        result = run_settlement_postgres(
            repo,
            now,
            interval_minutes=10,
            payout_decimals=8,
            reward_fetcher=lambda _start, _end: Decimal("0"),
            defer_on_zero_reward=True,
            use_work_accrual=False,
        )

        assert result.status == "deferred"
        assert repo.upsert_calls == 1
        assert repo.update_calls >= 2
