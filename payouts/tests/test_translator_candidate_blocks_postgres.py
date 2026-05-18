from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.postgres_db import make_postgres_engine, make_postgres_session_factory, resolve_postgres_database_url
from app.postgres_repositories import PostgresLedgerRepository, translator_candidate_blocks
from app.postgres_schema import metadata


def _make_repository() -> tuple[PostgresLedgerRepository, object, str]:
    pytest.importorskip("psycopg")

    configured_url = os.getenv("POSTGRES_LEDGER_TEST_DATABASE_URL")
    if not configured_url:
        pytest.skip("POSTGRES_LEDGER_TEST_DATABASE_URL is not configured")
    database_url = resolve_postgres_database_url(configured_url)
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


def _event(blockhash: str, found_time: datetime) -> dict:
    return {
        "found_time": found_time,
        "found_time_unix": int(found_time.timestamp()),
        "blockhash": blockhash,
        "worker_identity": "baveetstudy.miner2",
        "channel_id": 3,
        "job_id": "job-1",
        "extranonce2": "0a0b0c0d",
        "ntime": "65000001",
        "nonce": "00000000",
        "version": "20000000",
        "prev_hash": "00" * 32,
        "nbits": "2200ffff",
        "source": "sv1_capture_proxy",
        "proof_type": "translator_submit_reconstructed_block_hash",
        "raw_submit_json": {"method": "mining.submit"},
        "raw_job_json": {"method": "mining.notify"},
    }


def test_schema_metadata_includes_translator_candidate_blocks_table_and_indexes() -> None:
    assert translator_candidate_blocks.name in metadata.tables
    assert {column.name for column in translator_candidate_blocks.columns} == {
        "id",
        "found_time",
        "found_time_unix",
        "blockhash",
        "worker_identity",
        "channel_id",
        "job_id",
        "extranonce2",
        "ntime",
        "nonce",
        "version",
        "prev_hash",
        "nbits",
        "source",
        "proof_type",
        "raw_submit_json",
        "raw_job_json",
        "created_at",
    }
    assert {index.name for index in translator_candidate_blocks.indexes} == {
        "ix_translator_candidate_blocks_found_time",
        "ix_translator_candidate_blocks_worker_identity_found_time",
    }
    constraint_names = {constraint.name for constraint in translator_candidate_blocks.constraints}
    assert "ck_translator_candidate_blocks_blockhash_lower_hex" in constraint_names
    assert "uq_translator_candidate_blocks_blockhash" in constraint_names


def test_repository_insert_is_idempotent_on_blockhash() -> None:
    repository, base_engine, schema_name = _make_repository()
    try:
        found_time = datetime(2026, 5, 6, 17, 47, 45, tzinfo=UTC)
        blockhash = "0" * 63 + "1"

        first = repository.insert_translator_candidate_block(_event(blockhash, found_time))
        second = repository.insert_translator_candidate_block(
            _event(blockhash, found_time + timedelta(seconds=5))
        )

        assert second["id"] == first["id"]
        assert repository.get_translator_candidate_block_by_hash(blockhash)["id"] == first["id"]
    finally:
        _drop_schema(base_engine, schema_name)


def test_repository_list_filters_by_found_time_and_get_by_blockhash_works() -> None:
    repository, base_engine, schema_name = _make_repository()
    try:
        base_time = datetime(2026, 5, 6, 17, 0, 0, tzinfo=UTC)
        early_hash = "0" * 63 + "1"
        in_range_hash = "0" * 63 + "2"
        late_hash = "0" * 63 + "3"
        repository.insert_translator_candidate_block(_event(early_hash, base_time))
        repository.insert_translator_candidate_block(
            _event(in_range_hash, base_time + timedelta(minutes=30))
        )
        repository.insert_translator_candidate_block(
            _event(late_hash, base_time + timedelta(hours=2))
        )

        rows = repository.list_translator_candidate_blocks(
            start_time=base_time + timedelta(minutes=10),
            end_time=base_time + timedelta(hours=1),
            limit=10,
            order="asc",
        )

        assert [row["blockhash"] for row in rows] == [in_range_hash]
        assert repository.get_translator_candidate_block_by_hash(in_range_hash)["job_id"] == "job-1"
    finally:
        _drop_schema(base_engine, schema_name)
