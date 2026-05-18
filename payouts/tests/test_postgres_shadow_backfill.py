from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys
import uuid

from app.db import Base, make_engine, make_session_factory
from app.models import MetricSnapshot, Settlement, SnapshotBlock, User, UserPayout
from app.postgres_shadow_compare import compare_postgres_shadow_settlement


def _load_backfill_module():
    module_name = "test_backfill_postgres_shadow_script"
    if module_name in sys.modules:
        return sys.modules[module_name]

    script_path = Path(__file__).resolve().parents[1] / "scripts" / "backfill_postgres_shadow.py"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _make_case_dir() -> Path:
    return Path(__file__).resolve().parents[1]


class FakeBackfillRepository:
    def __init__(self) -> None:
        self._next_ids = {
            "user": 1,
            "identity": 1,
            "settlement": 1,
            "block": 1,
            "block_reward": 1,
            "user_work": 1,
            "credit": 1,
            "settlement_block": 1,
        }
        self.users_by_username: dict[str, dict[str, object]] = {}
        self.identities_by_value: dict[str, dict[str, object]] = {}
        self.settlements_by_window: dict[tuple[datetime, datetime], dict[str, object]] = {}
        self.blocks_by_hash: dict[str, dict[str, object]] = {}
        self.block_rewards_by_hash: dict[str, dict[str, object]] = {}
        self.settlement_blocks_by_hash: dict[str, dict[str, object]] = {}
        self.user_work_by_key: dict[tuple[int, int], dict[str, object]] = {}
        self.credits_by_key: dict[tuple[int, int], dict[str, object]] = {}
        self.write_calls: list[str] = []

    def _next_id(self, key: str) -> int:
        current = self._next_ids[key]
        self._next_ids[key] += 1
        return current

    def upsert_user(self, username: str, **kwargs) -> dict[str, object]:
        self.write_calls.append(f"upsert_user:{username}")
        existing = self.users_by_username.get(username)
        if existing is not None:
            existing.update({"status": kwargs.get("status", "active")})
            return existing
        row = {
            "id": self._next_id("user"),
            "username": username,
            "status": kwargs.get("status", "active"),
        }
        self.users_by_username[username] = row
        return row

    def get_user_by_username(self, username: str) -> dict[str, object] | None:
        return self.users_by_username.get(username)

    def upsert_miner_identity(self, user_id: int, identity: str, **kwargs) -> dict[str, object]:
        self.write_calls.append(f"upsert_miner_identity:{identity}")
        existing = self.identities_by_value.get(identity)
        if existing is not None:
            existing.update(
                {
                    "user_id": user_id,
                    "worker_name": kwargs.get("worker_name"),
                    "status": kwargs.get("status", "active"),
                }
            )
            return existing
        row = {
            "id": self._next_id("identity"),
            "user_id": user_id,
            "identity": identity,
            "worker_name": kwargs.get("worker_name"),
            "status": kwargs.get("status", "active"),
        }
        self.identities_by_value[identity] = row
        return row

    def get_miner_identity(self, identity: str) -> dict[str, object] | None:
        return self.identities_by_value.get(identity)

    def upsert_settlement_window(self, **kwargs) -> dict[str, object]:
        self.write_calls.append("upsert_settlement_window")
        key = (kwargs["work_window_start"], kwargs["work_window_end"])
        existing = self.settlements_by_window.get(key)
        if existing is not None:
            existing.update(kwargs)
            return existing
        row = {"id": self._next_id("settlement"), **kwargs}
        self.settlements_by_window[key] = row
        return row

    def get_settlement_window_by_range(
        self,
        *,
        work_window_start: datetime,
        work_window_end: datetime,
    ) -> dict[str, object] | None:
        return self.settlements_by_window.get((work_window_start, work_window_end))

    def upsert_block_found(self, **kwargs) -> dict[str, object]:
        blockhash = str(kwargs["blockhash"])
        self.write_calls.append(f"upsert_block_found:{blockhash}")
        existing = self.blocks_by_hash.get(blockhash)
        if existing is not None:
            existing.update(kwargs)
            return existing
        row = {"id": self._next_id("block"), **kwargs}
        self.blocks_by_hash[blockhash] = row
        return row

    def get_block_found(self, blockhash: str) -> dict[str, object] | None:
        return self.blocks_by_hash.get(blockhash)

    def upsert_block_reward(self, **kwargs) -> dict[str, object]:
        blockhash = str(kwargs["blockhash"])
        self.write_calls.append(f"upsert_block_reward:{blockhash}")
        existing = self.block_rewards_by_hash.get(blockhash)
        if existing is not None:
            existing.update(kwargs)
            return existing
        row = {"id": self._next_id("block_reward"), **kwargs}
        self.block_rewards_by_hash[blockhash] = row
        return row

    def get_block_reward(self, blockhash: str) -> dict[str, object] | None:
        return self.block_rewards_by_hash.get(blockhash)

    def link_settlement_block(self, **kwargs) -> dict[str, object]:
        blockhash = str(kwargs["blockhash"])
        self.write_calls.append(f"link_settlement_block:{blockhash}")
        existing = self.settlement_blocks_by_hash.get(blockhash)
        if existing is not None:
            if (
                int(existing["settlement_id"]) != int(kwargs["settlement_id"])
                or int(existing["reward_sats"]) != int(kwargs["reward_sats"])
            ):
                raise ValueError(
                    f"blockhash {blockhash} is already linked to settlement {existing['settlement_id']}"
                )
            return existing
        row = {"id": self._next_id("settlement_block"), **kwargs}
        self.settlement_blocks_by_hash[blockhash] = row
        return row

    def get_settlement_block(self, blockhash: str) -> dict[str, object] | None:
        return self.settlement_blocks_by_hash.get(blockhash)

    def upsert_settlement_user_work(self, **kwargs) -> dict[str, object]:
        key = (int(kwargs["settlement_id"]), int(kwargs["user_id"]))
        self.write_calls.append(f"upsert_settlement_user_work:{key[0]}:{key[1]}")
        existing = self.user_work_by_key.get(key)
        if existing is not None:
            existing.update(kwargs)
            return existing
        row = {"id": self._next_id("user_work"), **kwargs}
        self.user_work_by_key[key] = row
        return row

    def get_settlement_user_work(self, *, settlement_id: int, user_id: int) -> dict[str, object] | None:
        return self.user_work_by_key.get((int(settlement_id), int(user_id)))

    def upsert_settlement_user_credit(self, **kwargs) -> dict[str, object]:
        key = (int(kwargs["settlement_id"]), int(kwargs["user_id"]))
        self.write_calls.append(f"upsert_settlement_user_credit:{key[0]}:{key[1]}")
        existing = self.credits_by_key.get(key)
        if existing is not None:
            existing.update(kwargs)
            return existing
        row = {"id": self._next_id("credit"), **kwargs}
        self.credits_by_key[key] = row
        return row

    def get_settlement_user_credit(
        self,
        *,
        settlement_id: int,
        user_id: int,
    ) -> dict[str, object] | None:
        return self.credits_by_key.get((int(settlement_id), int(user_id)))

    def list_settlement_user_credits_with_users(self, settlement_id: int) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for (row_settlement_id, user_id), row in sorted(self.credits_by_key.items()):
            if row_settlement_id != int(settlement_id):
                continue
            user = next(user for user in self.users_by_username.values() if int(user["id"]) == user_id)
            rows.append(
                {
                    **row,
                    "username": user["username"],
                    "created_at": row.get("created_at"),
                }
            )
        rows.sort(key=lambda item: (str(item["username"]), int(item["id"])))
        return rows

    def list_settlement_blocks(self, settlement_id: int) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for blockhash, link_row in self.settlement_blocks_by_hash.items():
            if int(link_row["settlement_id"]) != int(settlement_id):
                continue
            block_row = self.blocks_by_hash[blockhash]
            rows.append(
                {
                    "id": link_row["id"],
                    "settlement_id": link_row["settlement_id"],
                    "blockhash": blockhash,
                    "reward_sats": link_row["reward_sats"],
                    "found_at": block_row["found_at"],
                    "channel_id": block_row.get("channel_id"),
                    "worker_identity": block_row.get("worker_identity"),
                    "source": block_row.get("source"),
                }
            )
        rows.sort(key=lambda item: (item["found_at"], int(item["id"])))
        return rows


class ReadOnlyFakeBackfillRepository(FakeBackfillRepository):
    def _no_write(self, name: str):
        raise AssertionError(f"Unexpected write call attempted: {name}")

    def upsert_user(self, username: str, **kwargs):
        _ = (username, kwargs)
        self._no_write("upsert_user")

    def upsert_miner_identity(self, user_id: int, identity: str, **kwargs):
        _ = (user_id, identity, kwargs)
        self._no_write("upsert_miner_identity")

    def upsert_settlement_window(self, **kwargs):
        _ = kwargs
        self._no_write("upsert_settlement_window")

    def upsert_block_found(self, **kwargs):
        _ = kwargs
        self._no_write("upsert_block_found")

    def upsert_block_reward(self, **kwargs):
        _ = kwargs
        self._no_write("upsert_block_reward")

    def link_settlement_block(self, **kwargs):
        _ = kwargs
        self._no_write("link_settlement_block")

    def upsert_settlement_user_work(self, **kwargs):
        _ = kwargs
        self._no_write("upsert_settlement_user_work")

    def upsert_settlement_user_credit(self, **kwargs):
        _ = kwargs
        self._no_write("upsert_settlement_user_credit")


def _seed_sqlite_history(case_dir: Path):
    db_file = case_dir / f"postgres_shadow_backfill_{uuid.uuid4().hex}.db"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)

    settlement_period_end = datetime(2026, 5, 1, 12, 0, 0)
    work_window_start = datetime(2026, 5, 1, 8, 30, 0)
    work_window_end = datetime(2026, 5, 1, 8, 40, 0)

    with Session() as session:
        user = User(username="alice")
        session.add(user)
        session.flush()

        session.add(
            MetricSnapshot(
                channel_id=7,
                identity="alice.rig1",
                accepted_shares_total=0,
                accepted_work_total=Decimal("0"),
                shares_rejected_total=0,
                created_at=work_window_start - timedelta(minutes=1),
            )
        )
        session.add(
            MetricSnapshot(
                channel_id=7,
                identity="alice.rig1",
                accepted_shares_total=5,
                accepted_work_total=Decimal("100.00000000"),
                shares_rejected_total=0,
                created_at=work_window_end - timedelta(minutes=1),
            )
        )

        completed = Settlement(
            status="completed",
            period_start=settlement_period_end - timedelta(minutes=10),
            period_end=settlement_period_end,
            total_shares=5,
            total_work=Decimal("100.00000000"),
            pool_reward_btc=Decimal("0.50000000"),
        )
        session.add(completed)
        session.flush()

        session.add(
            UserPayout(
                settlement_id=completed.id,
                user_id=user.id,
                contribution_value=Decimal("100.00000000"),
                payout_fraction=Decimal("1.000000000000"),
                amount_btc=Decimal("0.50000000"),
                idempotency_key=f"settlement-{completed.id}-user-{user.id}",
                status="pending",
            )
        )
        session.add(
            SnapshotBlock(
                found_at=work_window_start + timedelta(minutes=5),
                channel_id=7,
                worker_identity="alice.rig1",
                blockhash="block-001",
                source="translator_log",
                reward_sats=50_000_000,
                reward_fetched_at=work_window_start + timedelta(minutes=6),
                settlement_id=completed.id,
            )
        )

        pending = Settlement(
            status="pending",
            period_start=settlement_period_end,
            period_end=settlement_period_end + timedelta(minutes=10),
            total_shares=0,
            total_work=Decimal("0"),
            pool_reward_btc=Decimal("0"),
        )
        session.add(pending)
        session.commit()

    return {
        "db_file": db_file,
        "session_factory": Session,
        "completed_settlement_id": 1,
        "pending_settlement_id": 2,
        "work_window_start": work_window_start.replace(tzinfo=UTC),
        "work_window_end": work_window_end.replace(tzinfo=UTC),
        "settlement_run_at": settlement_period_end.replace(tzinfo=UTC),
    }


def _configure_shadow_env(monkeypatch, seeded) -> None:
    monkeypatch.setenv("DB_PATH", str(seeded["db_file"]))
    monkeypatch.setenv("ENABLE_BLOCK_EVENT_REWARDS", "true")
    monkeypatch.setenv("PAYOUT_INTERVAL_MINUTES", "10")
    monkeypatch.setenv("MATURITY_WINDOW_MINUTES", "200")
    monkeypatch.delenv("POSTGRES_LEDGER_DATABASE_URL", raising=False)


def test_backfill_dry_run_does_not_write(monkeypatch) -> None:
    seeded = _seed_sqlite_history(_make_case_dir())
    _configure_shadow_env(monkeypatch, seeded)
    module = _load_backfill_module()
    repository = ReadOnlyFakeBackfillRepository()

    with seeded["session_factory"]() as session:
        summary = module.backfill_postgres_shadow(
            session,
            repository,
            settlement_id=seeded["completed_settlement_id"],
            write=False,
        )

    assert summary["dry_run_count"] == 1
    assert summary["inserted_count"] == 0
    assert summary["mismatched_count"] == 0
    assert summary["settlement_results"][0]["status"] == "dry_run"


def test_backfill_one_completed_settlement_writes_expected_records(monkeypatch) -> None:
    seeded = _seed_sqlite_history(_make_case_dir())
    _configure_shadow_env(monkeypatch, seeded)
    module = _load_backfill_module()
    repository = FakeBackfillRepository()

    with seeded["session_factory"]() as session:
        summary = module.backfill_postgres_shadow(
            session,
            repository,
            settlement_id=seeded["completed_settlement_id"],
            write=True,
        )

    assert summary["inserted_count"] == 1
    assert summary["matched_count"] == 1
    assert len(repository.settlements_by_window) == 1
    assert len(repository.users_by_username) == 1
    assert len(repository.identities_by_value) == 1
    assert len(repository.blocks_by_hash) == 1
    assert len(repository.block_rewards_by_hash) == 1
    assert len(repository.settlement_blocks_by_hash) == 1
    assert len(repository.user_work_by_key) == 1
    assert len(repository.credits_by_key) == 1

    settlement_row = repository.get_settlement_window_by_range(
        work_window_start=seeded["work_window_start"],
        work_window_end=seeded["work_window_end"],
    )
    assert settlement_row is not None
    assert int(settlement_row["total_reward_sats"]) == 50_000_000
    assert settlement_row["settlement_run_at"] == seeded["settlement_run_at"]

    user_row = repository.get_user_by_username("alice")
    assert user_row is not None
    user_work = repository.get_settlement_user_work(
        settlement_id=int(settlement_row["id"]),
        user_id=int(user_row["id"]),
    )
    assert user_work is not None
    assert int(user_work["share_delta"]) == 5
    assert Decimal(str(user_work["work_delta"])) == Decimal("100.00000000")

    credit = repository.get_settlement_user_credit(
        settlement_id=int(settlement_row["id"]),
        user_id=int(user_row["id"]),
    )
    assert credit is not None
    assert int(credit["amount_sats"]) == 50_000_000


def test_backfill_is_idempotent_when_run_twice(monkeypatch) -> None:
    seeded = _seed_sqlite_history(_make_case_dir())
    _configure_shadow_env(monkeypatch, seeded)
    module = _load_backfill_module()
    repository = FakeBackfillRepository()

    with seeded["session_factory"]() as session:
        first = module.backfill_postgres_shadow(
            session,
            repository,
            settlement_id=seeded["completed_settlement_id"],
            write=True,
        )
        second = module.backfill_postgres_shadow(
            session,
            repository,
            settlement_id=seeded["completed_settlement_id"],
            write=True,
        )

    assert first["inserted_count"] == 1
    assert second["already_present_count"] == 1
    assert second["matched_count"] == 1
    assert len(repository.settlements_by_window) == 1
    assert len(repository.user_work_by_key) == 1
    assert len(repository.credits_by_key) == 1
    assert second["settlement_results"][0]["status"] == "already_present"


def test_backfill_skips_incomplete_settlements_safely(monkeypatch) -> None:
    seeded = _seed_sqlite_history(_make_case_dir())
    _configure_shadow_env(monkeypatch, seeded)
    module = _load_backfill_module()
    repository = FakeBackfillRepository()

    with seeded["session_factory"]() as session:
        summary = module.backfill_postgres_shadow(
            session,
            repository,
            settlement_id=seeded["pending_settlement_id"],
            write=True,
        )

    assert summary["skipped_count"] == 1
    assert summary["inserted_count"] == 0
    assert summary["settlement_results"][0]["status"] == "skipped"
    assert "not eligible" in summary["settlement_results"][0]["reason"]


def test_backfill_detects_conflict_and_reports_it(monkeypatch) -> None:
    seeded = _seed_sqlite_history(_make_case_dir())
    _configure_shadow_env(monkeypatch, seeded)
    module = _load_backfill_module()
    repository = FakeBackfillRepository()

    settlement_row = repository.upsert_settlement_window(
        settlement_run_at=seeded["settlement_run_at"],
        work_window_start=seeded["work_window_start"],
        work_window_end=seeded["work_window_end"],
        maturity_offset_minutes=200,
        status="completed",
        total_reward_sats=49_000_000,
        total_work=Decimal("100.00000000"),
        total_shares=5,
        completed_at=seeded["settlement_run_at"],
    )
    user_row = repository.upsert_user("alice")
    repository.upsert_settlement_user_work(
        settlement_id=int(settlement_row["id"]),
        user_id=int(user_row["id"]),
        share_delta=5,
        work_delta=Decimal("100.00000000"),
        payout_fraction=Decimal("1.000000000000"),
    )
    repository.upsert_settlement_user_credit(
        settlement_id=int(settlement_row["id"]),
        user_id=int(user_row["id"]),
        amount_sats=49_000_000,
        idempotency_key="settlement-1-user-1",
        status="pending",
    )

    with seeded["session_factory"]() as session:
        summary = module.backfill_postgres_shadow(
            session,
            repository,
            settlement_id=seeded["completed_settlement_id"],
            write=False,
        )

    assert summary["mismatched_count"] == 1
    result = summary["settlement_results"][0]
    assert result["status"] == "mismatched"
    assert result["forceable_conflicts"]
    assert result["comparison_status"] in {"not_found", "mismatched"}


def test_backfill_verification_reports_matched_after_successful_backfill(monkeypatch) -> None:
    seeded = _seed_sqlite_history(_make_case_dir())
    _configure_shadow_env(monkeypatch, seeded)
    module = _load_backfill_module()
    repository = FakeBackfillRepository()

    with seeded["session_factory"]() as session:
        summary = module.backfill_postgres_shadow(
            session,
            repository,
            settlement_id=seeded["completed_settlement_id"],
            write=True,
        )
        comparison, status_code = compare_postgres_shadow_settlement(
            session,
            seeded["completed_settlement_id"],
            repository=repository,
        )

    assert summary["matched_count"] == 1
    assert status_code == 200
    assert comparison["comparison_status"] == "matched"
    assert comparison["mismatches"] == []
