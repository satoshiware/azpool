from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterator

import psycopg
from psycopg import errors as pg_errors

from payouts.collector.app.config import PoolInstanceConfig
from payouts.collector.app.delta import DeltaComputation, SnapshotCounters
from payouts.collector.app.identity import IdentityMapping
from payouts.collector.app.pool_client import NormalizedChannel


@dataclass(frozen=True)
class PreviousSnapshot:
    shares_accepted: Decimal
    share_work_sum: Decimal
    last_share_sequence_number: int | None
    observed_at: datetime


class DatabaseError(RuntimeError):
    """Raised when collector database operations fail."""


@contextmanager
def connect(database_url: str) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(database_url)
    try:
        yield conn
    finally:
        conn.close()


def fetch_active_identity_mappings(conn: psycopg.Connection) -> list[IdentityMapping]:
    """Load active identity mappings; return empty list if migration not applied yet."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, sc_node_id, match_type, match_value
                FROM sc_node_identity_mappings
                WHERE status = 'active'
                ORDER BY id
                """
            )
            rows = cur.fetchall()
    except pg_errors.UndefinedTable:
        return []

    return [
        IdentityMapping(
            id=int(row[0]),
            sc_node_id=str(row[1]),
            match_type=str(row[2]),
            match_value=str(row[3]),
        )
        for row in rows
    ]


def ensure_pool_instance(conn: psycopg.Connection, pool: PoolInstanceConfig) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pool_instances (id, display_name, monitoring_base_url)
            VALUES (%s, %s, %s)
            ON CONFLICT (id) DO UPDATE
            SET display_name = EXCLUDED.display_name,
                monitoring_base_url = EXCLUDED.monitoring_base_url,
                updated_at = now()
            """,
            (pool.id, pool.display_name or pool.id, pool.base_url),
        )


def start_collector_run(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pool_collector_runs (status)
            VALUES ('running')
            RETURNING id
            """
        )
        row = cur.fetchone()
        if row is None:
            raise DatabaseError("failed to create collector run row")
        return int(row[0])


def finish_collector_run(
    conn: psycopg.Connection,
    run_id: int,
    *,
    status: str,
    pools_checked: int,
    snapshots_written: int,
    deltas_written: int,
    resets_detected: int,
    error_message: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE pool_collector_runs
            SET finished_at = now(),
                status = %s,
                pools_checked = %s,
                snapshots_written = %s,
                deltas_written = %s,
                resets_detected = %s,
                error_message = %s
            WHERE id = %s
            """,
            (
                status,
                pools_checked,
                snapshots_written,
                deltas_written,
                resets_detected,
                error_message,
                run_id,
            ),
        )


def fetch_previous_snapshot(
    conn: psycopg.Connection,
    *,
    pool_instance_id: str,
    client_id: int,
    channel_type: str,
    channel_id: int,
) -> PreviousSnapshot | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT shares_accepted, share_work_sum, last_share_sequence_number, observed_at
            FROM pool_channel_snapshots
            WHERE pool_instance_id = %s
              AND client_id = %s
              AND channel_type = %s
              AND channel_id = %s
            ORDER BY observed_at DESC, id DESC
            LIMIT 1
            """,
            (pool_instance_id, client_id, channel_type, channel_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return PreviousSnapshot(
            shares_accepted=Decimal(str(row[0])),
            share_work_sum=Decimal(str(row[1])),
            last_share_sequence_number=row[2],
            observed_at=row[3],
        )


def insert_snapshot(
    conn: psycopg.Connection,
    *,
    pool_instance_id: str,
    channel: NormalizedChannel,
    observed_at: datetime,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pool_channel_snapshots (
              pool_instance_id,
              client_id,
              channel_type,
              channel_id,
              user_identity,
              sc_node_id,
              shares_accepted,
              share_work_sum,
              last_share_sequence_number,
              blocks_found,
              observed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                pool_instance_id,
                channel.client_id,
                channel.channel_type,
                channel.channel_id,
                channel.user_identity,
                channel.sc_node_id,
                channel.shares_accepted,
                channel.share_work_sum,
                channel.last_share_sequence_number,
                channel.blocks_found,
                observed_at,
            ),
        )


def insert_delta(
    conn: psycopg.Connection,
    *,
    pool_instance_id: str,
    client_id: int,
    channel_type: str,
    channel_id: int,
    user_identity: str,
    sc_node_id: str | None,
    delta: DeltaComputation,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pool_share_work_deltas (
              pool_instance_id,
              client_id,
              channel_type,
              channel_id,
              user_identity,
              sc_node_id,
              accepted_delta,
              work_delta,
              from_sequence_number,
              to_sequence_number,
              observed_from,
              observed_to,
              reset_detected,
              idempotency_key
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            (
                pool_instance_id,
                client_id,
                channel_type,
                channel_id,
                user_identity,
                sc_node_id,
                delta.accepted_delta,
                delta.work_delta,
                delta.from_sequence_number,
                delta.to_sequence_number,
                delta.observed_from,
                delta.observed_to,
                delta.reset_detected,
                delta.idempotency_key,
            ),
        )
        return cur.fetchone() is not None


def counters_from_previous(previous: PreviousSnapshot) -> SnapshotCounters:
    return SnapshotCounters(
        shares_accepted=previous.shares_accepted,
        share_work_sum=previous.share_work_sum,
        last_share_sequence_number=previous.last_share_sequence_number,
        observed_at=previous.observed_at,
    )
