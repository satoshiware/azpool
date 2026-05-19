"""
Tests for Phase 1 Slices A, B, C - Postgres-first runtime de-SQLite.

Tests verify that:
- Slice A: main.py read functions use Postgres when enabled
- Slice B: poller.py writes blocks to Postgres
- Slice C: audit.py reads from Postgres when enabled
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import load_settings
from app.db import Base, make_engine, make_session_factory
from app.models import MetricSnapshot, SnapshotBlock, User, UserPayout, Settlement
from app.postgres_db import make_postgres_engine, make_postgres_session_factory, resolve_postgres_database_url
from app.postgres_repositories import PostgresLedgerRepository
from app.postgres_schema import metadata as pg_metadata
from app.poller import upsert_blocks_found_postgres, _normalize_snapshot_block_row
from app.audit import _build_snapshot_alignment, _build_payout_rows


def _make_postgres_repository() -> tuple[PostgresLedgerRepository, object, str]:
    """Create a test Postgres repository with isolated schema."""
    pytest.importorskip("psycopg")

    database_url = resolve_postgres_database_url(os.getenv("POSTGRES_LEDGER_TEST_DATABASE_URL"))
    base_engine = make_postgres_engine(database_url)

    try:
        with base_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable for Phase 1 tests: {exc}")

    schema_name = f"phase1_test_{uuid.uuid4().hex}"
    with base_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    schema_engine = make_postgres_engine(database_url, schema=schema_name)
    pg_metadata.create_all(schema_engine)

    repository = PostgresLedgerRepository(make_postgres_session_factory(schema_engine))
    return repository, base_engine, schema_name


def _drop_postgres_schema(base_engine, schema_name: str) -> None:
    """Clean up test schema."""
    with base_engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))


@pytest.fixture
def sqlite_session(tmp_path: Path):
    """Create a test SQLite session."""
    db_file = tmp_path / "phase1_test.db"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    SessionFactory = make_session_factory(engine)
    with SessionFactory() as s:
        yield s


@pytest.fixture
def postgres_repo():
    """Create a test Postgres repository."""
    repo, base_engine, schema_name = _make_postgres_repository()
    yield repo
    _drop_postgres_schema(base_engine, schema_name)


# ============================================================================
# SLICE A TESTS: main.py read functions with Postgres
# ============================================================================

class TestSliceALoadBlockRowsBySettlement:
    """Test _load_block_rows_by_settlement with Postgres."""

    def test_load_blocks_from_postgres_when_enabled(self, postgres_repo, monkeypatch):
        """Verify blocks are loaded from Postgres when primary session enabled."""
        from app.main import _load_block_rows_by_settlement
        
        # Setup Postgres test data
        user_row = postgres_repo.upsert_user("test_user")
        user_id = int(user_row["id"])
        
        now = datetime.now(UTC)
        settlement = postgres_repo.upsert_settlement_window(
            sqlite_settlement_id=None,
            settlement_run_at=now,
            work_window_start=now - timedelta(hours=1),
            work_window_end=now,
            maturity_offset_minutes=0,
            status="open",
            total_reward_sats=50000,
            total_work=Decimal("100.0"),
            total_shares=1000,
        )
        settlement_id = int(settlement["id"])
        
        # Insert blocks into Postgres
        block1 = postgres_repo.upsert_block_found(
            blockhash="abc123",
            found_at=now,
            channel_id=1,
            worker_identity="test_user.rig1",
            source="test",
        )
        
        block2 = postgres_repo.upsert_block_found(
            blockhash="def456",
            found_at=now + timedelta(seconds=10),
            channel_id=2,
            worker_identity="test_user.rig2",
            source="test",
        )
        
        postgres_repo.upsert_block_reward(
            blockhash="abc123",
            reward_sats=25000,
            fetched_at=now,
        )
        
        postgres_repo.upsert_block_reward(
            blockhash="def456",
            reward_sats=25000,
            fetched_at=now,
        )
        
        postgres_repo.link_settlement_block(
            settlement_id=settlement_id,
            blockhash="abc123",
            reward_sats=25000,
        )
        
        postgres_repo.link_settlement_block(
            settlement_id=settlement_id,
            blockhash="def456",
            reward_sats=25000,
        )
        
        # Mock settings and session
        mock_settings = mock.MagicMock()
        mock_settings.postgres_primary_session_enabled = True
        
        # Mock the load_settings to return our test settings
        with mock.patch('app.main.load_settings', return_value=mock_settings):
            mock_sqlite_session = mock.MagicMock()
            result = _load_block_rows_by_settlement(mock_sqlite_session, [settlement_id])
        
        # Verify Postgres path was taken (SQLite session not called)
        assert settlement_id in result
        assert len(result[settlement_id]) == 2
        
        blocks = result[settlement_id]
        blockhashes = {b["blockhash"] for b in blocks}
        assert blockhashes == {"abc123", "def456"}
        
        # Verify reward_sats in result
        for block in blocks:
            assert int(block["reward_sats"]) == 25000
            assert Decimal(block["reward_btc"]) == Decimal("0.00025000")


# ============================================================================
# SLICE B TESTS: poller.py block writes to Postgres
# ============================================================================

class TestSliceBUpsertBlocksFoundPostgres:
    """Test upsert_blocks_found_postgres function."""

    def test_upsert_blocks_found_postgres_creates_rows(self, postgres_repo):
        """Verify blocks are upserted to Postgres blocks_found."""
        block_rows = [
            {
                "found_at": datetime.now(UTC),
                "channel_id": 1,
                "worker_identity": "alice.rig1",
                "blockhash": "hash1",
                "source": "translator_blocks_api",
                "reward_sats": None,
                "reward_fetched_at": None,
            },
            {
                "found_at": datetime.now(UTC) + timedelta(seconds=5),
                "channel_id": 2,
                "worker_identity": "bob.rig2",
                "blockhash": "hash2",
                "source": "translator_blocks_api",
                "reward_sats": None,
                "reward_fetched_at": None,
            },
        ]
        
        created = upsert_blocks_found_postgres(
            postgres_repo,
            block_rows,
            source_default="translator_blocks_api",
        )
        
        assert created == 2
        
        # Verify blocks were inserted
        block1 = postgres_repo.get_block_found("hash1")
        block2 = postgres_repo.get_block_found("hash2")
        
        assert block1 is not None
        assert block1["blockhash"] == "hash1"
        assert int(block1["channel_id"]) == 1
        assert block1["worker_identity"] == "alice.rig1"
        
        assert block2 is not None
        assert block2["blockhash"] == "hash2"
        assert int(block2["channel_id"]) == 2
        assert block2["worker_identity"] == "bob.rig2"

    def test_upsert_blocks_found_postgres_dedupes(self, postgres_repo):
        """Verify duplicate blocks are not inserted twice."""
        now = datetime.now(UTC)
        block_rows_1 = [
            {
                "found_at": now,
                "channel_id": 1,
                "worker_identity": "alice.rig1",
                "blockhash": "hash1",
                "source": "translator_blocks_api",
                "reward_sats": None,
                "reward_fetched_at": None,
            },
        ]
        
        block_rows_2 = [
            {
                "found_at": now,
                "channel_id": 1,
                "worker_identity": "alice.rig1",
                "blockhash": "hash1",
                "source": "translator_blocks_api",
                "reward_sats": None,
                "reward_fetched_at": None,
            },
        ]
        
        created_1 = upsert_blocks_found_postgres(
            postgres_repo,
            block_rows_1,
            source_default="translator_blocks_api",
        )
        created_2 = upsert_blocks_found_postgres(
            postgres_repo,
            block_rows_2,
            source_default="translator_blocks_api",
        )
        
        assert created_1 == 1
        assert created_2 == 0  # Second insert should dedupe


# ============================================================================
# SLICE C TESTS: audit.py Postgres reads
# ============================================================================

class TestSliceCBuildSnapshotAlignment:
    """Test _build_snapshot_alignment with Postgres."""

    def test_build_snapshot_alignment_uses_postgres_when_enabled(self, postgres_repo, monkeypatch):
        """Verify snapshot alignment reads from Postgres when primary session enabled."""
        
        # Setup Postgres test data
        now = datetime.now(UTC)
        
        # Insert raw miner snapshots
        snap1 = postgres_repo.create_raw_miner_snapshot(
            captured_at=now - timedelta(hours=1),
            identity="alice.rig1",
            accepted_shares_total=100,
            accepted_work_total=Decimal("50.0"),
            channel_id=1,
            source="test",
        )
        
        snap2 = postgres_repo.create_raw_miner_snapshot(
            captured_at=now - timedelta(minutes=30),
            identity="alice.rig1",
            accepted_shares_total=150,
            accepted_work_total=Decimal("75.0"),
            channel_id=1,
            source="test",
        )
        
        snap3 = postgres_repo.create_raw_miner_snapshot(
            captured_at=now - timedelta(minutes=5),
            identity="alice.rig1",
            accepted_shares_total=200,
            accepted_work_total=Decimal("100.0"),
            channel_id=1,
            source="test",
        )
        
        period_start = now - timedelta(hours=2)
        period_end = now
        
        # Mock settings to enable Postgres
        mock_settings = mock.MagicMock()
        mock_settings.postgres_primary_session_enabled = True
        
        with mock.patch('app.audit.load_settings', return_value=mock_settings):
            mock_sqlite_session = mock.MagicMock()
            result = _build_snapshot_alignment(mock_sqlite_session, period_start, period_end)
        
        # Verify result structure
        assert "miners" in result
        assert "miner_count" in result
        assert "total_share_delta" in result
        assert "total_work_delta" in result
        
        # Verify miner data was captured
        assert result["miner_count"] > 0
        miners = result["miners"]
        assert len(miners) > 0
        
        # Verify deltas
        miner = miners[0]
        assert miner["identity"] == "alice.rig1"
        assert miner["share_delta"] > 0  # Should have accumulated shares


class TestSliceCBuildPayoutRows:
    """Test _build_payout_rows with Postgres."""

    def test_build_payout_rows_uses_postgres_when_enabled(self, postgres_repo, monkeypatch):
        """Verify payout rows are read from Postgres when primary session enabled."""
        
        # Setup Postgres test data
        now = datetime.now(UTC)
        
        user = postgres_repo.upsert_user("test_user")
        user_id = int(user["id"])
        
        settlement = postgres_repo.upsert_settlement_window(
            sqlite_settlement_id=None,
            settlement_run_at=now,
            work_window_start=now - timedelta(hours=1),
            work_window_end=now,
            maturity_offset_minutes=0,
            status="open",
            total_reward_sats=100000,
            total_work=Decimal("100.0"),
            total_shares=1000,
        )
        settlement_id = int(settlement["id"])
        
        # Create settlement user credit
        credit = postgres_repo.upsert_settlement_user_credit(
            settlement_id=settlement_id,
            user_id=user_id,
            amount_sats=50000,
            idempotency_key="test_key_1",
            status="pending",
        )
        
        # Create settlement user work
        work = postgres_repo.upsert_settlement_user_work(
            settlement_id=settlement_id,
            user_id=user_id,
            share_delta=500,
            work_delta=Decimal("50.0"),
            payout_fraction=Decimal("0.5"),
        )
        
        # Mock settings to enable Postgres
        mock_settings = mock.MagicMock()
        mock_settings.postgres_primary_session_enabled = True
        
        with mock.patch('app.audit.load_settings', return_value=mock_settings):
            mock_sqlite_session = mock.MagicMock()
            result = _build_payout_rows(mock_sqlite_session, settlement_id)
        
        # Verify result structure
        assert len(result) > 0
        
        payout_row = result[0]
        assert payout_row["username"] == "test_user"
        assert payout_row["status"] == "pending"
        
        # Verify amount in BTC
        amount_sats = 50000
        expected_btc = Decimal(str(amount_sats)) / Decimal("100000000")
        actual_btc = Decimal(payout_row["amount_btc"])
        assert actual_btc == expected_btc
        
        # Verify payout_fraction and contribution_value
        assert payout_row["payout_fraction"] == "0.500000000000"
        assert payout_row["contribution_value"] == "500.00000000"


# ============================================================================
# FALLBACK TESTS: Verify SQLite fallback when Postgres unavailable
# ============================================================================

class TestSliceAFallbackToSQLite:
    """Test that Slice A functions fall back to SQLite if Postgres fails."""

    def test_load_blocks_falls_back_to_sqlite_on_postgres_error(self, sqlite_session):
        """Verify blocks load from SQLite when Postgres is unavailable."""
        from app.main import _load_block_rows_by_settlement
        
        # Setup SQLite test data
        settlement = Settlement(
            pool_interval_start="2024-01-01 00:00:00",
            pool_interval_end="2024-01-01 01:00:00",
            reward_btc=Decimal("6.25"),
            total_work=Decimal("1000.0"),
            total_shares=10000,
            submitted_at=datetime.now(),
            status="completed",
        )
        sqlite_session.add(settlement)
        sqlite_session.flush()
        settlement_id = settlement.id
        
        block = SnapshotBlock(
            settlement_id=settlement_id,
            blockhash="abc123",
            found_at=datetime.now(),
            channel_id=1,
            worker_identity="test.rig1",
            source="test",
            reward_sats=100,
        )
        sqlite_session.add(block)
        sqlite_session.commit()
        
        # Mock settings to enable Postgres but make the call fail
        mock_settings = mock.MagicMock()
        mock_settings.postgres_primary_session_enabled = True
        
        with mock.patch('app.main.load_settings', return_value=mock_settings):
            with mock.patch('app.main.PostgresLedgerRepository', side_effect=Exception("Postgres unavailable")):
                result = _load_block_rows_by_settlement(sqlite_session, [settlement_id])
        
        # Verify fallback to SQLite worked
        assert settlement_id in result
        assert len(result[settlement_id]) == 1
        assert result[settlement_id][0]["blockhash"] == "abc123"


# ============================================================================
# INTEGRATION TESTS
# ============================================================================

class TestPhase1Integration:
    """Integration tests for Phase 1 slices together."""

    def test_blocks_written_to_postgres_can_be_read_back(self, postgres_repo):
        """Verify end-to-end: write blocks to Postgres, read them back."""
        now = datetime.now(UTC)
        
        # Step 1: Write blocks using Slice B
        block_rows = [
            {
                "found_at": now,
                "channel_id": 1,
                "worker_identity": "miner1.rig1",
                "blockhash": "block1",
                "source": "translator_blocks_api",
                "reward_sats": None,
                "reward_fetched_at": None,
            },
        ]
        
        created = upsert_blocks_found_postgres(
            postgres_repo,
            block_rows,
            source_default="translator_blocks_api",
        )
        assert created == 1
        
        # Step 2: Set up settlement with blocks
        settlement = postgres_repo.upsert_settlement_window(
            sqlite_settlement_id=None,
            settlement_run_at=now,
            work_window_start=now - timedelta(hours=1),
            work_window_end=now,
            maturity_offset_minutes=0,
            status="open",
            total_reward_sats=50000,
            total_work=Decimal("100.0"),
            total_shares=1000,
        )
        settlement_id = int(settlement["id"])
        
        # Add reward
        postgres_repo.upsert_block_reward(
            blockhash="block1",
            reward_sats=25000,
            fetched_at=now,
        )
        
        # Link block to settlement
        postgres_repo.link_settlement_block(
            settlement_id=settlement_id,
            blockhash="block1",
            reward_sats=25000,
        )
        
        # Step 3: Read blocks back using Slice A method pattern
        blocks = postgres_repo.list_settlement_blocks(settlement_id)
        
        assert len(blocks) == 1
        assert blocks[0]["blockhash"] == "block1"
        assert int(blocks[0]["reward_sats"]) == 25000
