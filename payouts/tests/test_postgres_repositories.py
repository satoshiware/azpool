from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.postgres_db import make_postgres_engine, make_postgres_session_factory, resolve_postgres_database_url
from app.postgres_repositories import PostgresLedgerRepository, _worker_name_from_identity
from app.postgres_schema import metadata


def _make_repository() -> tuple[PostgresLedgerRepository, object, str]:
    pytest.importorskip("psycopg")

    database_url = resolve_postgres_database_url(os.getenv("POSTGRES_LEDGER_TEST_DATABASE_URL"))
    base_engine = make_postgres_engine(database_url)

    try:
        with base_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"Postgres is not reachable for repository smoke test: {exc}")

    schema_name = f"ledger_test_{uuid.uuid4().hex}"
    with base_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    schema_engine = make_postgres_engine(database_url, schema=schema_name)
    metadata.create_all(schema_engine)

    repository = PostgresLedgerRepository(make_postgres_session_factory(schema_engine))
    return repository, base_engine, schema_name


def _drop_schema(base_engine, schema_name: str) -> None:
    with base_engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))


def test_schema_metadata_includes_summary_snapshot_tables() -> None:
    assert "summary_snapshot" in metadata.tables
    assert "summary_snapshot_miner" in metadata.tables

    summary_columns = {column.name for column in metadata.tables["summary_snapshot"].columns}
    assert summary_columns == {
        "id",
        "settlement_id",
        "payout_period_start",
        "payout_period_end",
        "contribution_window_start",
        "contribution_window_end",
        "snapshot_count",
        "accepted_shares_sum",
        "accepted_work_sum",
        "created_at",
    }

    miner_columns = {column.name for column in metadata.tables["summary_snapshot_miner"].columns}
    assert miner_columns == {
        "id",
        "summary_snapshot_id",
        "worker_identity",
        "worker_name",
        "channel_id",
        "snapshot_count",
        "accepted_shares_sum",
        "accepted_work_sum",
        "created_at",
    }


def test_worker_name_from_identity_parsing() -> None:
    assert _worker_name_from_identity("alice.rig1") == "rig1"
    assert _worker_name_from_identity("alice.rig.a") == "rig.a"
    assert _worker_name_from_identity("alice") is None
    assert _worker_name_from_identity(" ") is None
    assert _worker_name_from_identity("bob.") is None


def test_schema_metadata_includes_runtime_state_tables() -> None:
    assert "carry_state" in metadata.tables
    assert "work_accrual_bucket" in metadata.tables
    assert "payout_events" in metadata.tables
    assert "block_counter_state" in metadata.tables

    carry_columns = {column.name for column in metadata.tables["carry_state"].columns}
    assert carry_columns == {
        "id",
        "bucket",
        "carry_btc",
        "updated_at",
    }

    accrual_columns = {column.name for column in metadata.tables["work_accrual_bucket"].columns}
    assert accrual_columns == {
        "id",
        "user_id",
        "accumulated_work",
        "updated_at",
    }

    payout_event_columns = {column.name for column in metadata.tables["payout_events"].columns}
    assert payout_event_columns == {
        "id",
        "settlement_credit_id",
        "payload_json",
        "status",
        "created_at",
    }

    block_counter_columns = {column.name for column in metadata.tables["block_counter_state"].columns}
    assert block_counter_columns == {
        "id",
        "channel_id",
        "last_blocks_found_total",
        "updated_at",
    }


def test_schema_metadata_includes_sqlite_settlement_mapping_column() -> None:
    settlement_columns = {column.name for column in metadata.tables["settlement_windows"].columns}
    assert "sqlite_settlement_id" in settlement_columns


def test_postgres_repository_block_counter_state_roundtrip() -> None:
    repository, base_engine, schema_name = _make_repository()
    try:
        first = repository.upsert_block_counter_state(
            channel_id=7,
            last_blocks_found_total=12,
        )
        assert first["channel_id"] == 7
        assert first["last_blocks_found_total"] == 12

        second = repository.upsert_block_counter_state(
            channel_id=7,
            last_blocks_found_total=18,
        )
        assert second["id"] == first["id"]
        assert second["last_blocks_found_total"] == 18

        rows = repository.list_block_counter_state()
        assert len(rows) == 1
        assert rows[0]["channel_id"] == 7
        assert rows[0]["last_blocks_found_total"] == 18
    finally:
        _drop_schema(base_engine, schema_name)


def test_postgres_repository_summary_methods_empty_state() -> None:
    repository, base_engine, schema_name = _make_repository()
    try:
        detail = repository.get_latest_settlement_detail()
        summary = repository.get_service_metrics_summary()

        assert detail is None
        assert summary["settlements_total"] == 0
        assert summary["payouts_sent_total"] == 0
        assert summary["payout_failures_total"] == 0
        assert summary["last_settlement_timestamp"] is None
    finally:
        _drop_schema(base_engine, schema_name)


def test_postgres_repository_smoke() -> None:
    repository, base_engine, schema_name = _make_repository()
    try:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        user = repository.upsert_user("alice")
        same_user = repository.upsert_user("alice")
        assert same_user["id"] == user["id"]

        miner = repository.upsert_miner_identity(
            user_id=user["id"],
            identity="alice.rig1",
            worker_name="rig1",
        )
        assert repository.get_miner_identity("alice.rig1")["id"] == miner["id"]

        snapshot = repository.create_raw_miner_snapshot(
            captured_at=now,
            channel_id=7,
            identity="alice.rig1",
            accepted_shares_total=25,
            accepted_work_total=Decimal("1250.5000000000000000"),
            rejected_shares_total=1,
            raw_payload={"accepted": 25},
        )
        assert repository.get_raw_miner_snapshot(snapshot["id"])["identity"] == "alice.rig1"

        work_delta = repository.create_miner_work_delta(
            identity="alice.rig1",
            channel_id=7,
            from_snapshot_id=snapshot["id"],
            to_snapshot_id=snapshot["id"],
            interval_start=now - timedelta(minutes=10),
            interval_end=now,
            share_delta=25,
            work_delta=Decimal("1250.5000000000000000"),
        )
        assert repository.get_miner_work_delta(work_delta["id"])["share_delta"] == 25

        block = repository.upsert_block_found(
            blockhash="blockhash-001",
            found_at=now + timedelta(minutes=1),
            channel_id=7,
            worker_identity="alice.rig1",
        )
        assert repository.get_block_found("blockhash-001")["id"] == block["id"]

        reward = repository.upsert_block_reward(
            blockhash="blockhash-001",
            reward_sats=187_500_000,
            fetched_at=now + timedelta(minutes=2),
        )
        assert repository.get_block_reward("blockhash-001")["reward_sats"] == 187_500_000

        settlement = repository.upsert_settlement_window(
            sqlite_settlement_id=101,
            settlement_run_at=now + timedelta(hours=4),
            work_window_start=now - timedelta(hours=8, minutes=200),
            work_window_end=now - timedelta(minutes=200),
            maturity_offset_minutes=200,
            status="pending",
            total_reward_sats=187_500_000,
            total_work=Decimal("1250.5000000000000000"),
            total_shares=25,
        )
        assert settlement["sqlite_settlement_id"] == 101
        same_settlement = repository.get_settlement_window_by_range(
            work_window_start=settlement["work_window_start"],
            work_window_end=settlement["work_window_end"],
        )
        assert same_settlement["id"] == settlement["id"]
        assert same_settlement["sqlite_settlement_id"] == 101

        settlement_block = repository.link_settlement_block(
            settlement_id=settlement["id"],
            blockhash="blockhash-001",
            reward_sats=187_500_000,
        )
        same_settlement_block = repository.link_settlement_block(
            settlement_id=settlement["id"],
            blockhash="blockhash-001",
            reward_sats=187_500_000,
        )
        assert same_settlement_block["id"] == settlement_block["id"]

        user_work = repository.upsert_settlement_user_work(
            settlement_id=settlement["id"],
            user_id=user["id"],
            share_delta=25,
            work_delta=Decimal("1250.5000000000000000"),
            payout_fraction=Decimal("1.000000000000000000"),
        )
        assert repository.get_settlement_user_work(
            settlement_id=settlement["id"],
            user_id=user["id"],
        )["id"] == user_work["id"]

        user_credit = repository.upsert_settlement_user_credit(
            settlement_id=settlement["id"],
            user_id=user["id"],
            amount_sats=187_500_000,
            idempotency_key=f"settlement-{settlement['id']}-user-{user['id']}",
            status="pending",
        )
        assert repository.get_settlement_user_credit(
            settlement_id=settlement["id"],
            user_id=user["id"],
        )["id"] == user_credit["id"]

        ledger_entry = repository.create_account_ledger_entry(
            user_id=user["id"],
            entry_type="settlement_credit",
            amount_sats=187_500_000,
            direction="credit",
            settlement_credit_id=user_credit["id"],
            memo="initial credit",
        )
        assert repository.get_account_ledger_entry(ledger_entry["id"])["settlement_credit_id"] == user_credit["id"]

        balance = repository.set_account_balance(
            user_id=user["id"],
            balance_sats=187_500_000,
            updated_at=now + timedelta(minutes=3),
        )
        assert balance["balance_sats"] == 187_500_000
        assert repository.get_account_balance(user["id"])["balance_sats"] == 187_500_000

        audit_event = repository.create_audit_event(
            event_type="settlement_created",
            entity_type="settlement_window",
            entity_id=str(settlement["id"]),
            payload={"settlement_id": settlement["id"]},
            payload_hash="payload-1",
        )
        assert repository.get_audit_event(audit_event["id"])["event_type"] == "settlement_created"

        cursor = repository.upsert_service_cursor(
            cursor_name="translator_blocks_found",
            cursor_value="cursor-001",
            updated_at=now + timedelta(minutes=4),
        )
        assert repository.get_service_cursor("translator_blocks_found")["id"] == cursor["id"]
    finally:
        _drop_schema(base_engine, schema_name)


def test_phase_c_snapshot_compaction_orchestration() -> None:
    """Test Phase C: orchestration of snapshot compaction after settlement write."""
    repository, base_engine, schema_name = _make_repository()
    try:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        
        # Set up: create users, miners, and raw snapshots across multiple windows
        user = repository.upsert_user("alice")
        repository.upsert_miner_identity(
            user_id=user["id"],
            identity="alice.rig1",
            worker_name="rig1",
        )
        
        # Create raw snapshots in window 1 (oldest, should be pruned)
        window_1_start = now - timedelta(hours=16)
        window_1_end = now - timedelta(hours=12)
        snapshot_1 = repository.create_raw_miner_snapshot(
            captured_at=window_1_start + timedelta(minutes=5),
            channel_id=7,
            identity="alice.rig1",
            accepted_shares_total=100,
            accepted_work_total=Decimal("5000.0000000000000000"),
            rejected_shares_total=5,
            raw_payload={"accepted": 100},
        )
        
        # Create work delta for window 1
        repository.create_miner_work_delta(
            identity="alice.rig1",
            channel_id=7,
            from_snapshot_id=snapshot_1["id"],
            to_snapshot_id=snapshot_1["id"],
            interval_start=window_1_start,
            interval_end=window_1_end,
            share_delta=100,
            work_delta=Decimal("5000.0000000000000000"),
        )
        
        # Create raw snapshots in window 2
        window_2_start = now - timedelta(hours=12)
        window_2_end = now - timedelta(hours=8)
        snapshot_2 = repository.create_raw_miner_snapshot(
            captured_at=window_2_start + timedelta(minutes=5),
            channel_id=7,
            identity="alice.rig1",
            accepted_shares_total=150,
            accepted_work_total=Decimal("7500.0000000000000000"),
            rejected_shares_total=7,
            raw_payload={"accepted": 150},
        )
        
        repository.create_miner_work_delta(
            identity="alice.rig1",
            channel_id=7,
            from_snapshot_id=snapshot_2["id"],
            to_snapshot_id=snapshot_2["id"],
            interval_start=window_2_start,
            interval_end=window_2_end,
            share_delta=150,
            work_delta=Decimal("7500.0000000000000000"),
        )
        
        # Create raw snapshots in window 3
        window_3_start = now - timedelta(hours=8)
        window_3_end = now - timedelta(hours=4)
        snapshot_3 = repository.create_raw_miner_snapshot(
            captured_at=window_3_start + timedelta(minutes=5),
            channel_id=7,
            identity="alice.rig1",
            accepted_shares_total=200,
            accepted_work_total=Decimal("10000.0000000000000000"),
            rejected_shares_total=10,
            raw_payload={"accepted": 200},
        )
        
        repository.create_miner_work_delta(
            identity="alice.rig1",
            channel_id=7,
            from_snapshot_id=snapshot_3["id"],
            to_snapshot_id=snapshot_3["id"],
            interval_start=window_3_start,
            interval_end=window_3_end,
            share_delta=200,
            work_delta=Decimal("10000.0000000000000000"),
        )

        # Create raw snapshots in window 4 (current/settlement window)
        window_4_start = now - timedelta(hours=4)
        window_4_end = now
        snapshot_4 = repository.create_raw_miner_snapshot(
            captured_at=window_4_start + timedelta(minutes=5),
            channel_id=7,
            identity="alice.rig1",
            accepted_shares_total=250,
            accepted_work_total=Decimal("12500.0000000000000000"),
            rejected_shares_total=12,
            raw_payload={"accepted": 250},
        )

        repository.create_miner_work_delta(
            identity="alice.rig1",
            channel_id=7,
            from_snapshot_id=snapshot_4["id"],
            to_snapshot_id=snapshot_4["id"],
            interval_start=window_4_start,
            interval_end=window_4_end,
            share_delta=250,
            work_delta=Decimal("12500.0000000000000000"),
        )
        
        # Create settlement window
        settlement = repository.upsert_settlement_window(
            settlement_run_at=now,
            work_window_start=window_4_start,
            work_window_end=window_4_end,
            maturity_offset_minutes=200,
            status="completed",
            total_reward_sats=187_500_000,
            total_work=Decimal("12500.0000000000000000"),
            total_shares=250,
            completed_at=now,
        )
        
        # PHASE C ORCHESTRATION: Execute compaction steps
        # 1. Summarize raw snapshots for the settlement window
        aggregates = repository.summarize_raw_snapshots_for_window(
            contribution_window_start=window_4_start,
            contribution_window_end=window_4_end,
        )
        
        assert aggregates["accepted_shares_sum"] == 250
        assert aggregates["accepted_work_sum"] == Decimal("12500.0000000000000000")
        assert aggregates["snapshot_count"] == 1
        assert len(aggregates["miners"]) == 1
        miner_agg = aggregates["miners"][0]
        assert miner_agg["worker_identity"] == "alice.rig1"
        assert miner_agg["worker_name"] == "rig1"
        assert miner_agg["channel_id"] == 7
        
        # 2. Upsert summary header row (idempotent)
        summary_row = repository.upsert_summary_snapshot(
            settlement_id=settlement["id"],
            payout_period_start=window_4_start,
            payout_period_end=window_4_end,
            contribution_window_start=window_4_start,
            contribution_window_end=window_4_end,
            accepted_shares_sum=aggregates["accepted_shares_sum"],
            accepted_work_sum=aggregates["accepted_work_sum"],
            snapshot_count=aggregates["snapshot_count"],
        )
        summary_id = int(summary_row["id"])
        assert summary_id > 0
        
        # Verify idempotency: second upsert returns same summary_id
        summary_row_2 = repository.upsert_summary_snapshot(
            settlement_id=settlement["id"],
            payout_period_start=window_4_start,
            payout_period_end=window_4_end,
            contribution_window_start=window_4_start,
            contribution_window_end=window_4_end,
            accepted_shares_sum=aggregates["accepted_shares_sum"],
            accepted_work_sum=aggregates["accepted_work_sum"],
            snapshot_count=aggregates["snapshot_count"],
        )
        assert int(summary_row_2["id"]) == summary_id
        
        # 3. Replace summary miner rows
        repository.replace_summary_snapshot_miners(
            summary_snapshot_id=summary_id,
            miners=aggregates["miners"],
        )
        
        # 4. Prune raw snapshots keeping latest 3 windows
        prune_stats = repository.prune_raw_snapshot_windows(keep_latest_windows=3)
        
        # With 4 windows present, the oldest one should be pruned.
        assert prune_stats["pruned_window_count"] == 1
        assert prune_stats["deleted_snapshot_count"] >= 1
        assert prune_stats["deleted_delta_count"] >= 1
        
        with repository.session_factory() as session:
            all_snapshots = session.execute(
                text("SELECT id FROM raw_miner_snapshots ORDER BY captured_at")
            ).fetchall()
            assert len(all_snapshots) == 3
            snapshot_1_row = session.execute(
                text("SELECT id FROM raw_miner_snapshots WHERE id = :id"),
                {"id": int(snapshot_1["id"])},
            ).fetchone()
            assert snapshot_1_row is None
        
        # Verify summary rows were created
        with repository.session_factory() as session:
            summary_miners = session.execute(
                text("SELECT * FROM summary_snapshot_miner WHERE summary_snapshot_id = :id"),
                {"id": summary_id},
            ).fetchall()
            assert len(summary_miners) == 1
            assert summary_miners[0].worker_identity == "alice.rig1"
        
    finally:
        _drop_schema(base_engine, schema_name)
