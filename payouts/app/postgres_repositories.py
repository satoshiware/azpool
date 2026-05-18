from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import (
    TEXT,
    BigInteger,
    CheckConstraint,
    Column,
    and_,
    delete,
    func,
    Index,
    Integer,
    Select,
    Table,
    UniqueConstraint,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import sessionmaker

from app.postgres_schema import (
    account_balances,
    account_ledger_entries,
    audit_events,
    block_rewards,
    blocks_found,
    block_counter_state,
    carry_state,
    metadata,
    miner_identities,
    miner_work_deltas,
    payout_events,
    raw_miner_snapshots,
    service_cursors,
    settlement_blocks,
    summary_snapshot,
    summary_snapshot_miner,
    settlement_user_credits,
    settlement_user_work,
    settlement_windows,
    users,
    work_accrual_bucket,
)


translator_candidate_blocks = Table(
    "translator_candidate_blocks",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("found_time", TIMESTAMP(timezone=True), nullable=False),
    Column("found_time_unix", BigInteger, nullable=False),
    Column("blockhash", TEXT, nullable=False),
    Column("worker_identity", TEXT),
    Column("channel_id", Integer),
    Column("job_id", TEXT),
    Column("extranonce2", TEXT),
    Column("ntime", TEXT),
    Column("nonce", TEXT),
    Column("version", TEXT),
    Column("prev_hash", TEXT),
    Column("nbits", TEXT),
    Column("source", TEXT, nullable=False, server_default=text("'sv1_capture_proxy'")),
    Column(
        "proof_type",
        TEXT,
        nullable=False,
        server_default=text("'translator_submit_reconstructed_block_hash'"),
    ),
    Column("raw_submit_json", JSONB),
    Column("raw_job_json", JSONB),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint(
        "blockhash ~ '^[0-9a-f]{64}$'",
        name="ck_translator_candidate_blocks_blockhash_lower_hex",
    ),
    UniqueConstraint("blockhash", name="uq_translator_candidate_blocks_blockhash"),
)
Index("ix_translator_candidate_blocks_found_time", translator_candidate_blocks.c.found_time)
Index(
    "ix_translator_candidate_blocks_worker_identity_found_time",
    translator_candidate_blocks.c.worker_identity,
    translator_candidate_blocks.c.found_time,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _require_tzaware(name: str, value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _as_decimal(value: Decimal | int | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


def _clean_values(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _worker_name_from_identity(identity: str) -> str | None:
    value = (identity or "").strip()
    if not value or "." not in value:
        return None
    worker = value.split(".", 1)[1].strip()
    return worker or None


def _event_field(event: Mapping[str, Any] | Any, name: str) -> Any:
    if isinstance(event, Mapping):
        return event.get(name)
    return getattr(event, name, None)


def _translator_candidate_block_values(event: Mapping[str, Any] | Any) -> dict[str, Any]:
    found_time = _event_field(event, "found_time")
    _require_tzaware("found_time", found_time)
    created_at = _event_field(event, "created_at")
    _require_tzaware("created_at", created_at)

    blockhash = _event_field(event, "blockhash")
    if blockhash is None:
        raise ValueError("blockhash is required")

    found_time_unix = _event_field(event, "found_time_unix")
    if found_time_unix is None:
        found_time_unix = int(found_time.timestamp())

    return _clean_values(
        {
            "found_time": found_time,
            "found_time_unix": int(found_time_unix),
            "blockhash": str(blockhash).lower(),
            "worker_identity": _event_field(event, "worker_identity"),
            "channel_id": _event_field(event, "channel_id"),
            "job_id": _event_field(event, "job_id"),
            "extranonce2": _event_field(event, "extranonce2"),
            "ntime": _event_field(event, "ntime"),
            "nonce": _event_field(event, "nonce"),
            "version": _event_field(event, "version"),
            "prev_hash": _event_field(event, "prev_hash"),
            "nbits": _event_field(event, "nbits"),
            "source": _event_field(event, "source"),
            "proof_type": _event_field(event, "proof_type"),
            "raw_submit_json": _event_field(event, "raw_submit_json"),
            "raw_job_json": _event_field(event, "raw_job_json"),
            "created_at": created_at,
        }
    )


@dataclass
class PostgresLedgerRepository:
    session_factory: sessionmaker

    def get_metadata(self):
        return metadata

    def _execute_returning_one(self, statement) -> dict[str, Any]:
        with self.session_factory() as session:
            with session.begin():
                row = session.execute(statement).one()
            return _row_to_dict(row)

    def _select_one_or_none(self, statement: Select) -> dict[str, Any] | None:
        with self.session_factory() as session:
            row = session.execute(statement).first()
            return None if row is None else _row_to_dict(row)

    def _select_all(self, statement: Select) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.execute(statement).all()
            return [_row_to_dict(row) for row in rows]

    def upsert_user(
        self,
        username: str,
        *,
        status: str = "active",
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        _require_tzaware("created_at", created_at)
        values = _clean_values(
            {
                "username": username,
                "status": status,
                "created_at": created_at,
            }
        )
        statement = (
            pg_insert(users)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[users.c.username],
                set_={"status": status},
            )
            .returning(*users.c)
        )
        return self._execute_returning_one(statement)

    def get_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        return self._select_one_or_none(select(users).where(users.c.id == user_id))

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        return self._select_one_or_none(select(users).where(users.c.username == username))

    def upsert_miner_identity(
        self,
        user_id: int,
        identity: str,
        *,
        worker_name: str | None = None,
        status: str = "active",
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        _require_tzaware("created_at", created_at)
        values = _clean_values(
            {
                "user_id": user_id,
                "identity": identity,
                "worker_name": worker_name,
                "status": status,
                "created_at": created_at,
            }
        )
        statement = (
            pg_insert(miner_identities)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[miner_identities.c.identity],
                set_={
                    "user_id": user_id,
                    "worker_name": worker_name,
                    "status": status,
                },
            )
            .returning(*miner_identities.c)
        )
        return self._execute_returning_one(statement)

    def get_miner_identity(self, identity: str) -> dict[str, Any] | None:
        return self._select_one_or_none(
            select(miner_identities).where(miner_identities.c.identity == identity)
        )

    def create_raw_miner_snapshot(
        self,
        *,
        captured_at: datetime,
        identity: str,
        accepted_shares_total: int,
        accepted_work_total: Decimal | int | str,
        channel_id: int | None = None,
        rejected_shares_total: int = 0,
        source: str = "translator",
        raw_payload: dict[str, Any] | list[Any] | None = None,
    ) -> dict[str, Any]:
        _require_tzaware("captured_at", captured_at)
        statement = (
            raw_miner_snapshots.insert()
            .values(
                captured_at=captured_at,
                channel_id=channel_id,
                identity=identity,
                accepted_shares_total=accepted_shares_total,
                accepted_work_total=_as_decimal(accepted_work_total),
                rejected_shares_total=rejected_shares_total,
                source=source,
                raw_payload=raw_payload,
            )
            .returning(*raw_miner_snapshots.c)
        )
        return self._execute_returning_one(statement)

    def get_raw_miner_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        return self._select_one_or_none(
            select(raw_miner_snapshots).where(raw_miner_snapshots.c.id == snapshot_id)
        )

    def list_raw_miner_snapshot_counters_up_to(self, *, period_end: datetime) -> list[dict[str, Any]]:
        _require_tzaware("period_end", period_end)
        statement = (
            select(
                raw_miner_snapshots.c.identity,
                raw_miner_snapshots.c.channel_id,
                raw_miner_snapshots.c.accepted_shares_total,
                raw_miner_snapshots.c.accepted_work_total,
                raw_miner_snapshots.c.captured_at,
            )
            .where(raw_miner_snapshots.c.captured_at <= period_end)
            .order_by(
                raw_miner_snapshots.c.identity.asc(),
                raw_miner_snapshots.c.channel_id.asc(),
                raw_miner_snapshots.c.captured_at.asc(),
                raw_miner_snapshots.c.id.asc(),
            )
        )
        return self._select_all(statement)

    def create_miner_work_delta(
        self,
        *,
        identity: str,
        interval_start: datetime,
        interval_end: datetime,
        share_delta: int,
        work_delta: Decimal | int | str,
        channel_id: int | None = None,
        from_snapshot_id: int | None = None,
        to_snapshot_id: int | None = None,
        reset_detected: bool = False,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        _require_tzaware("interval_start", interval_start)
        _require_tzaware("interval_end", interval_end)
        _require_tzaware("created_at", created_at)
        statement = (
            miner_work_deltas.insert()
            .values(
                identity=identity,
                channel_id=channel_id,
                from_snapshot_id=from_snapshot_id,
                to_snapshot_id=to_snapshot_id,
                interval_start=interval_start,
                interval_end=interval_end,
                share_delta=share_delta,
                work_delta=_as_decimal(work_delta),
                reset_detected=reset_detected,
                **({"created_at": created_at} if created_at is not None else {}),
            )
            .returning(*miner_work_deltas.c)
        )
        return self._execute_returning_one(statement)

    def get_miner_work_delta(self, delta_id: int) -> dict[str, Any] | None:
        return self._select_one_or_none(
            select(miner_work_deltas).where(miner_work_deltas.c.id == delta_id)
        )

    def upsert_block_found(
        self,
        *,
        blockhash: str,
        found_at: datetime,
        channel_id: int | None = None,
        worker_identity: str | None = None,
        source: str = "translator_blocks_found",
        status: str = "found",
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        _require_tzaware("found_at", found_at)
        _require_tzaware("created_at", created_at)
        values = _clean_values(
            {
                "blockhash": blockhash,
                "found_at": found_at,
                "channel_id": channel_id,
                "worker_identity": worker_identity,
                "source": source,
                "status": status,
                "created_at": created_at,
            }
        )
        statement = (
            pg_insert(blocks_found)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[blocks_found.c.blockhash],
                set_={
                    "found_at": found_at,
                    "channel_id": channel_id,
                    "worker_identity": worker_identity,
                    "source": source,
                    "status": status,
                },
            )
            .returning(*blocks_found.c)
        )
        return self._execute_returning_one(statement)

    def get_block_found(self, blockhash: str) -> dict[str, Any] | None:
        return self._select_one_or_none(select(blocks_found).where(blocks_found.c.blockhash == blockhash))

    def insert_translator_candidate_block(self, event: Mapping[str, Any] | Any) -> dict[str, Any]:
        values = _translator_candidate_block_values(event)
        statement = (
            pg_insert(translator_candidate_blocks)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[translator_candidate_blocks.c.blockhash])
            .returning(*translator_candidate_blocks.c)
        )
        row = self._select_returned_or_none(statement)
        if row is not None:
            return row
        existing = self.get_translator_candidate_block_by_hash(values["blockhash"])
        if existing is None:
            raise RuntimeError("translator candidate block insert conflicted but no existing row was found")
        return existing

    def list_translator_candidate_blocks(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
        order: str = "desc",
    ) -> list[dict[str, Any]]:
        _require_tzaware("start_time", start_time)
        _require_tzaware("end_time", end_time)
        if limit < 1:
            raise ValueError("limit must be positive")

        normalized_order = order.lower()
        if normalized_order == "asc":
            order_by = (
                translator_candidate_blocks.c.found_time.asc(),
                translator_candidate_blocks.c.id.asc(),
            )
        elif normalized_order == "desc":
            order_by = (
                translator_candidate_blocks.c.found_time.desc(),
                translator_candidate_blocks.c.id.desc(),
            )
        else:
            raise ValueError("order must be asc or desc")

        statement = select(translator_candidate_blocks)
        if start_time is not None:
            statement = statement.where(translator_candidate_blocks.c.found_time >= start_time)
        if end_time is not None:
            statement = statement.where(translator_candidate_blocks.c.found_time < end_time)
        return self._select_all(statement.order_by(*order_by).limit(limit))

    def count_translator_candidate_blocks(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> int:
        _require_tzaware("start_time", start_time)
        _require_tzaware("end_time", end_time)

        statement = select(func.count(translator_candidate_blocks.c.id))
        if start_time is not None:
            statement = statement.where(translator_candidate_blocks.c.found_time >= start_time)
        if end_time is not None:
            statement = statement.where(translator_candidate_blocks.c.found_time < end_time)

        with self.session_factory() as session:
            return int(session.execute(statement).scalar_one() or 0)

    def get_translator_candidate_block_by_hash(self, blockhash: str) -> dict[str, Any] | None:
        return self._select_one_or_none(
            select(translator_candidate_blocks).where(
                translator_candidate_blocks.c.blockhash == blockhash.lower()
            )
        )

    def upsert_block_reward(
        self,
        *,
        blockhash: str,
        reward_sats: int,
        reward_source: str = "az_block_rewards",
        fetched_at: datetime | None = None,
    ) -> dict[str, Any]:
        _require_tzaware("fetched_at", fetched_at)
        values = _clean_values(
            {
                "blockhash": blockhash,
                "reward_sats": reward_sats,
                "reward_source": reward_source,
                "fetched_at": fetched_at,
            }
        )
        statement = (
            pg_insert(block_rewards)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[block_rewards.c.blockhash],
                set_=_clean_values(
                    {
                        "reward_sats": reward_sats,
                        "reward_source": reward_source,
                        "fetched_at": fetched_at,
                    }
                ),
            )
            .returning(*block_rewards.c)
        )
        return self._execute_returning_one(statement)

    def get_block_reward(self, blockhash: str) -> dict[str, Any] | None:
        return self._select_one_or_none(
            select(block_rewards).where(block_rewards.c.blockhash == blockhash)
        )

    def upsert_settlement_window(
        self,
        *,
        sqlite_settlement_id: int | None = None,
        settlement_run_at: datetime,
        work_window_start: datetime,
        work_window_end: datetime,
        maturity_offset_minutes: int,
        status: str = "pending",
        total_reward_sats: int = 0,
        total_work: Decimal | int | str = Decimal("0"),
        total_shares: int = 0,
        created_at: datetime | None = None,
        completed_at: datetime | None = None,
    ) -> dict[str, Any]:
        _require_tzaware("settlement_run_at", settlement_run_at)
        _require_tzaware("work_window_start", work_window_start)
        _require_tzaware("work_window_end", work_window_end)
        _require_tzaware("created_at", created_at)
        _require_tzaware("completed_at", completed_at)
        values = _clean_values(
            {
                "sqlite_settlement_id": sqlite_settlement_id,
                "status": status,
                "settlement_run_at": settlement_run_at,
                "work_window_start": work_window_start,
                "work_window_end": work_window_end,
                "maturity_offset_minutes": maturity_offset_minutes,
                "total_reward_sats": total_reward_sats,
                "total_work": _as_decimal(total_work),
                "total_shares": total_shares,
                "created_at": created_at,
                "completed_at": completed_at,
            }
        )
        update_values: dict[str, Any] = {
            "status": status,
            "settlement_run_at": settlement_run_at,
            "maturity_offset_minutes": maturity_offset_minutes,
            "total_reward_sats": total_reward_sats,
            "total_work": _as_decimal(total_work),
            "total_shares": total_shares,
            "completed_at": completed_at,
        }
        if sqlite_settlement_id is not None:
            update_values["sqlite_settlement_id"] = sqlite_settlement_id

        statement = (
            pg_insert(settlement_windows)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[
                    settlement_windows.c.work_window_start,
                    settlement_windows.c.work_window_end,
                ],
                set_=update_values,
            )
            .returning(*settlement_windows.c)
        )
        return self._execute_returning_one(statement)

    def update_settlement_window_by_id(
        self,
        *,
        settlement_id: int,
        sqlite_settlement_id: int | None = None,
        settlement_run_at: datetime | None = None,
        maturity_offset_minutes: int | None = None,
        status: str | None = None,
        total_reward_sats: int | None = None,
        total_work: Decimal | int | str | None = None,
        total_shares: int | None = None,
        completed_at: datetime | None = None,
    ) -> dict[str, Any]:
        _require_tzaware("settlement_run_at", settlement_run_at)
        _require_tzaware("completed_at", completed_at)
        values = _clean_values(
            {
                "sqlite_settlement_id": sqlite_settlement_id,
                "status": status,
                "settlement_run_at": settlement_run_at,
                "maturity_offset_minutes": maturity_offset_minutes,
                "total_reward_sats": total_reward_sats,
                "total_work": _as_decimal(total_work) if total_work is not None else None,
                "total_shares": total_shares,
                "completed_at": completed_at,
            }
        )
        if not values:
            return self.get_settlement_window_by_id(settlement_id) or {}

        statement = (
            update(settlement_windows)
            .where(settlement_windows.c.id == settlement_id)
            .values(**values)
            .returning(*settlement_windows.c)
        )
        return self._execute_returning_one(statement)

    def get_settlement_window_by_id(self, settlement_id: int) -> dict[str, Any] | None:
        return self._select_one_or_none(
            select(settlement_windows).where(settlement_windows.c.id == settlement_id)
        )

    def get_latest_settlement_window(self) -> dict[str, Any] | None:
        return self._select_one_or_none(
            select(settlement_windows)
            .order_by(settlement_windows.c.work_window_end.desc(), settlement_windows.c.id.desc())
            .limit(1)
        )

    def list_block_counter_state(self) -> list[dict[str, Any]]:
        return self._select_all(select(block_counter_state).order_by(block_counter_state.c.channel_id.asc()))

    def upsert_block_counter_state(
        self,
        *,
        channel_id: int,
        last_blocks_found_total: int,
        updated_at: datetime | None = None,
    ) -> dict[str, Any]:
        _require_tzaware("updated_at", updated_at)
        effective_updated_at = updated_at or utcnow()
        statement = (
            pg_insert(block_counter_state)
            .values(
                channel_id=channel_id,
                last_blocks_found_total=last_blocks_found_total,
                updated_at=effective_updated_at,
            )
            .on_conflict_do_update(
                index_elements=[block_counter_state.c.channel_id],
                set_={
                    "last_blocks_found_total": last_blocks_found_total,
                    "updated_at": effective_updated_at,
                },
            )
            .returning(*block_counter_state.c)
        )
        return self._execute_returning_one(statement)

    def get_latest_settlement_detail(self) -> dict[str, Any] | None:
        settlement = self.get_latest_settlement_window()
        if settlement is None:
            return None

        settlement_id = int(settlement.get("id") or 0)
        return {
            "settlement": settlement,
            "user_credits": self.list_settlement_user_credits_with_users(settlement_id),
            "user_work": self.list_settlement_user_work_with_users(settlement_id),
            "settlement_blocks": self.list_settlement_blocks(settlement_id),
        }

    def get_service_metrics_summary(self) -> dict[str, Any]:
        settlements_total = self._select_one_or_none(select(func.count(settlement_windows.c.id)))
        payouts_sent_total = self._select_one_or_none(
            select(func.count(settlement_user_credits.c.id)).where(settlement_user_credits.c.status == "sent")
        )
        payout_failures_total = self._select_one_or_none(
            select(func.count(payout_events.c.id)).where(payout_events.c.status == "pending_sent")
        )
        last_settlement_timestamp = self._select_one_or_none(select(func.max(settlement_windows.c.work_window_end)))

        return {
            "settlements_total": int(settlements_total or 0),
            "payouts_sent_total": int(payouts_sent_total or 0),
            "payout_failures_total": int(payout_failures_total or 0),
            "last_settlement_timestamp": last_settlement_timestamp,
        }

    def get_settlement_window_by_range(
        self,
        *,
        work_window_start: datetime,
        work_window_end: datetime,
    ) -> dict[str, Any] | None:
        _require_tzaware("work_window_start", work_window_start)
        _require_tzaware("work_window_end", work_window_end)
        return self._select_one_or_none(
            select(settlement_windows).where(
                settlement_windows.c.work_window_start == work_window_start,
                settlement_windows.c.work_window_end == work_window_end,
            )
        )

    def summarize_raw_snapshots_for_window(
        self,
        *,
        contribution_window_start: datetime,
        contribution_window_end: datetime,
    ) -> dict[str, Any]:
        _require_tzaware("contribution_window_start", contribution_window_start)
        _require_tzaware("contribution_window_end", contribution_window_end)

        with self.session_factory() as session:
            totals_row = session.execute(
                select(
                    func.count(raw_miner_snapshots.c.id),
                    func.coalesce(func.sum(raw_miner_snapshots.c.accepted_shares_total), 0),
                    func.coalesce(func.sum(raw_miner_snapshots.c.accepted_work_total), Decimal("0")),
                ).where(
                    raw_miner_snapshots.c.captured_at >= contribution_window_start,
                    raw_miner_snapshots.c.captured_at < contribution_window_end,
                )
            ).one()

            miner_rows = session.execute(
                select(
                    raw_miner_snapshots.c.identity,
                    raw_miner_snapshots.c.channel_id,
                    func.count(raw_miner_snapshots.c.id),
                    func.coalesce(func.sum(raw_miner_snapshots.c.accepted_shares_total), 0),
                    func.coalesce(func.sum(raw_miner_snapshots.c.accepted_work_total), Decimal("0")),
                )
                .where(
                    raw_miner_snapshots.c.captured_at >= contribution_window_start,
                    raw_miner_snapshots.c.captured_at < contribution_window_end,
                )
                .group_by(
                    raw_miner_snapshots.c.identity,
                    raw_miner_snapshots.c.channel_id,
                )
                .order_by(
                    raw_miner_snapshots.c.identity.asc(),
                    raw_miner_snapshots.c.channel_id.asc(),
                )
            ).all()

        miners: list[dict[str, Any]] = []
        for identity, channel_id, snapshot_count, shares_sum, work_sum in miner_rows:
            identity_value = str(identity)
            miners.append(
                {
                    "worker_identity": identity_value,
                    "worker_name": _worker_name_from_identity(identity_value),
                    "channel_id": channel_id,
                    "snapshot_count": int(snapshot_count or 0),
                    "accepted_shares_sum": int(shares_sum or 0),
                    "accepted_work_sum": _as_decimal(work_sum or 0),
                }
            )

        return {
            "snapshot_count": int(totals_row[0] or 0),
            "accepted_shares_sum": int(totals_row[1] or 0),
            "accepted_work_sum": _as_decimal(totals_row[2] or 0),
            "miners": miners,
        }

    def upsert_summary_snapshot(
        self,
        *,
        settlement_id: int,
        payout_period_start: datetime,
        payout_period_end: datetime,
        contribution_window_start: datetime,
        contribution_window_end: datetime,
        snapshot_count: int,
        accepted_shares_sum: int,
        accepted_work_sum: Decimal | int | str,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        _require_tzaware("payout_period_start", payout_period_start)
        _require_tzaware("payout_period_end", payout_period_end)
        _require_tzaware("contribution_window_start", contribution_window_start)
        _require_tzaware("contribution_window_end", contribution_window_end)
        _require_tzaware("created_at", created_at)
        statement = (
            pg_insert(summary_snapshot)
            .values(
                settlement_id=settlement_id,
                payout_period_start=payout_period_start,
                payout_period_end=payout_period_end,
                contribution_window_start=contribution_window_start,
                contribution_window_end=contribution_window_end,
                snapshot_count=snapshot_count,
                accepted_shares_sum=accepted_shares_sum,
                accepted_work_sum=_as_decimal(accepted_work_sum),
                **({"created_at": created_at} if created_at is not None else {}),
            )
            .on_conflict_do_update(
                index_elements=[summary_snapshot.c.settlement_id],
                set_={
                    "payout_period_start": payout_period_start,
                    "payout_period_end": payout_period_end,
                    "contribution_window_start": contribution_window_start,
                    "contribution_window_end": contribution_window_end,
                    "snapshot_count": snapshot_count,
                    "accepted_shares_sum": accepted_shares_sum,
                    "accepted_work_sum": _as_decimal(accepted_work_sum),
                },
            )
            .returning(*summary_snapshot.c)
        )
        return self._execute_returning_one(statement)

    def replace_summary_snapshot_miners(
        self,
        *,
        summary_snapshot_id: int,
        miners: list[dict[str, Any]],
        created_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        _require_tzaware("created_at", created_at)

        with self.session_factory() as session:
            with session.begin():
                session.execute(
                    delete(summary_snapshot_miner).where(
                        summary_snapshot_miner.c.summary_snapshot_id == summary_snapshot_id
                    )
                )

                if not miners:
                    return []

                rows: list[dict[str, Any]] = []
                for miner in miners:
                    values = {
                        "summary_snapshot_id": summary_snapshot_id,
                        "worker_identity": str(miner["worker_identity"]),
                        "worker_name": miner.get("worker_name"),
                        "channel_id": miner.get("channel_id"),
                        "snapshot_count": int(miner.get("snapshot_count", 0) or 0),
                        "accepted_shares_sum": int(miner.get("accepted_shares_sum", 0) or 0),
                        "accepted_work_sum": _as_decimal(miner.get("accepted_work_sum", 0) or 0),
                        **({"created_at": created_at} if created_at is not None else {}),
                    }
                    row = session.execute(
                        summary_snapshot_miner.insert()
                        .values(**values)
                        .returning(*summary_snapshot_miner.c)
                    ).one()
                    rows.append(_row_to_dict(row))
        return rows

    def list_summary_snapshots(self, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        return self._select_all(
            select(summary_snapshot)
            .order_by(summary_snapshot.c.contribution_window_end.desc(), summary_snapshot.c.id.desc())
            .limit(limit)
        )

    def get_summary_snapshot_by_settlement_id(self, settlement_id: int) -> dict[str, Any] | None:
        return self._select_one_or_none(
            select(summary_snapshot).where(summary_snapshot.c.settlement_id == settlement_id)
        )

    def list_summary_snapshot_miners(self, summary_snapshot_id: int) -> list[dict[str, Any]]:
        return self._select_all(
            select(summary_snapshot_miner)
            .where(summary_snapshot_miner.c.summary_snapshot_id == summary_snapshot_id)
            .order_by(
                summary_snapshot_miner.c.worker_identity.asc(),
                summary_snapshot_miner.c.channel_id.asc(),
                summary_snapshot_miner.c.id.asc(),
            )
        )

    def prune_raw_snapshot_windows(self, *, keep_latest_windows: int = 3) -> dict[str, Any]:
        if keep_latest_windows < 1:
            raise ValueError("keep_latest_windows must be positive")

        with self.session_factory() as session:
            with session.begin():
                windows = session.execute(
                    select(
                        settlement_windows.c.work_window_start,
                        settlement_windows.c.work_window_end,
                    )
                    .order_by(
                        settlement_windows.c.work_window_end.desc(),
                        settlement_windows.c.id.desc(),
                    )
                ).all()

                prune_windows = windows[keep_latest_windows:]
                if not prune_windows:
                    return {
                        "deleted_snapshot_count": 0,
                        "deleted_delta_count": 0,
                        "pruned_window_count": 0,
                    }

                deleted_snapshot_count = 0
                deleted_delta_count = 0

                for window_start, window_end in prune_windows:
                    snapshot_id_subquery = (
                        select(raw_miner_snapshots.c.id)
                        .where(
                            raw_miner_snapshots.c.captured_at >= window_start,
                            raw_miner_snapshots.c.captured_at < window_end,
                        )
                    )

                    delete_deltas_result = session.execute(
                        delete(miner_work_deltas).where(
                            (miner_work_deltas.c.from_snapshot_id.in_(snapshot_id_subquery))
                            | (miner_work_deltas.c.to_snapshot_id.in_(snapshot_id_subquery))
                            | (
                                and_(
                                    miner_work_deltas.c.interval_start >= window_start,
                                    miner_work_deltas.c.interval_end <= window_end,
                                )
                            )
                        )
                    )
                    deleted_delta_count += int(delete_deltas_result.rowcount or 0)

                    delete_snapshots_result = session.execute(
                        delete(raw_miner_snapshots).where(
                            raw_miner_snapshots.c.captured_at >= window_start,
                            raw_miner_snapshots.c.captured_at < window_end,
                        )
                    )
                    deleted_snapshot_count += int(delete_snapshots_result.rowcount or 0)

        return {
            "deleted_snapshot_count": deleted_snapshot_count,
            "deleted_delta_count": deleted_delta_count,
            "pruned_window_count": len(prune_windows),
        }

    def link_settlement_block(
        self,
        *,
        settlement_id: int,
        blockhash: str,
        reward_sats: int,
    ) -> dict[str, Any]:
        statement = (
            pg_insert(settlement_blocks)
            .values(
                settlement_id=settlement_id,
                blockhash=blockhash,
                reward_sats=reward_sats,
            )
            .on_conflict_do_nothing(index_elements=[settlement_blocks.c.blockhash])
            .returning(*settlement_blocks.c)
        )
        row = self._select_returned_or_none(statement)
        if row is not None:
            return row
        existing = self.get_settlement_block(blockhash)
        if existing is None:
            raise RuntimeError("settlement block insert conflicted but no existing row was found")
        if (
            existing["settlement_id"] != settlement_id
            or existing["reward_sats"] != reward_sats
        ):
            raise ValueError(
                f"blockhash {blockhash} is already linked to settlement {existing['settlement_id']}"
            )
        return existing

    def _select_returned_or_none(self, statement) -> dict[str, Any] | None:
        with self.session_factory() as session:
            with session.begin():
                row = session.execute(statement).first()
            return None if row is None else _row_to_dict(row)

    def get_settlement_block(self, blockhash: str) -> dict[str, Any] | None:
        return self._select_one_or_none(
            select(settlement_blocks).where(settlement_blocks.c.blockhash == blockhash)
        )

    def upsert_settlement_user_work(
        self,
        *,
        settlement_id: int,
        user_id: int,
        share_delta: int,
        work_delta: Decimal | int | str,
        payout_fraction: Decimal | int | str,
    ) -> dict[str, Any]:
        statement = (
            pg_insert(settlement_user_work)
            .values(
                settlement_id=settlement_id,
                user_id=user_id,
                share_delta=share_delta,
                work_delta=_as_decimal(work_delta),
                payout_fraction=_as_decimal(payout_fraction),
            )
            .on_conflict_do_update(
                index_elements=[
                    settlement_user_work.c.settlement_id,
                    settlement_user_work.c.user_id,
                ],
                set_={
                    "share_delta": share_delta,
                    "work_delta": _as_decimal(work_delta),
                    "payout_fraction": _as_decimal(payout_fraction),
                },
            )
            .returning(*settlement_user_work.c)
        )
        return self._execute_returning_one(statement)

    def get_settlement_user_work(
        self,
        *,
        settlement_id: int,
        user_id: int,
    ) -> dict[str, Any] | None:
        return self._select_one_or_none(
            select(settlement_user_work).where(
                settlement_user_work.c.settlement_id == settlement_id,
                settlement_user_work.c.user_id == user_id,
            )
        )

    def upsert_settlement_user_credit(
        self,
        *,
        settlement_id: int,
        user_id: int,
        amount_sats: int,
        idempotency_key: str,
        status: str = "pending",
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        _require_tzaware("created_at", created_at)
        values = _clean_values(
            {
                "settlement_id": settlement_id,
                "user_id": user_id,
                "amount_sats": amount_sats,
                "idempotency_key": idempotency_key,
                "status": status,
                "created_at": created_at,
            }
        )
        statement = (
            pg_insert(settlement_user_credits)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[
                    settlement_user_credits.c.settlement_id,
                    settlement_user_credits.c.user_id,
                ],
                set_={
                    "amount_sats": amount_sats,
                    "idempotency_key": idempotency_key,
                    "status": status,
                },
            )
            .returning(*settlement_user_credits.c)
        )
        return self._execute_returning_one(statement)

    def get_settlement_user_credit(
        self,
        *,
        settlement_id: int,
        user_id: int,
    ) -> dict[str, Any] | None:
        return self._select_one_or_none(
            select(settlement_user_credits).where(
                settlement_user_credits.c.settlement_id == settlement_id,
                settlement_user_credits.c.user_id == user_id,
            )
        )

    def list_settlement_user_credits_with_users(self, settlement_id: int) -> list[dict[str, Any]]:
        return self._select_all(
            select(
                settlement_user_credits.c.id,
                settlement_user_credits.c.settlement_id,
                settlement_user_credits.c.user_id,
                settlement_user_credits.c.amount_sats,
                settlement_user_credits.c.idempotency_key,
                settlement_user_credits.c.status,
                settlement_user_credits.c.created_at,
                users.c.username,
            )
            .select_from(
                settlement_user_credits.join(
                    users,
                    users.c.id == settlement_user_credits.c.user_id,
                )
            )
            .where(settlement_user_credits.c.settlement_id == settlement_id)
            .order_by(users.c.username.asc(), settlement_user_credits.c.id.asc())
        )

    def list_settlement_user_work_with_users(self, settlement_id: int) -> list[dict[str, Any]]:
        return self._select_all(
            select(
                settlement_user_work.c.id,
                settlement_user_work.c.settlement_id,
                settlement_user_work.c.user_id,
                settlement_user_work.c.share_delta,
                settlement_user_work.c.work_delta,
                settlement_user_work.c.payout_fraction,
                users.c.username,
            )
            .select_from(
                settlement_user_work.join(
                    users,
                    users.c.id == settlement_user_work.c.user_id,
                )
            )
            .where(settlement_user_work.c.settlement_id == settlement_id)
            .order_by(users.c.username.asc(), settlement_user_work.c.id.asc())
        )

    def list_settlement_blocks(self, settlement_id: int) -> list[dict[str, Any]]:
        return self._select_all(
            select(
                settlement_blocks.c.id,
                settlement_blocks.c.settlement_id,
                settlement_blocks.c.blockhash,
                settlement_blocks.c.reward_sats,
                blocks_found.c.found_at,
                blocks_found.c.channel_id,
                blocks_found.c.worker_identity,
                blocks_found.c.source,
            )
            .select_from(
                settlement_blocks.join(
                    blocks_found,
                    blocks_found.c.blockhash == settlement_blocks.c.blockhash,
                )
            )
            .where(settlement_blocks.c.settlement_id == settlement_id)
            .order_by(blocks_found.c.found_at.asc(), settlement_blocks.c.id.asc())
        )

    def list_settlement_blocks_by_ids(self, settlement_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        """Get settlement blocks grouped by settlement_id."""
        if not settlement_ids:
            return {}
        
        rows = self._select_all(
            select(
                settlement_blocks.c.id,
                settlement_blocks.c.settlement_id,
                settlement_blocks.c.blockhash,
                settlement_blocks.c.reward_sats,
                blocks_found.c.found_at,
                blocks_found.c.channel_id,
                blocks_found.c.worker_identity,
                blocks_found.c.source,
            )
            .select_from(
                settlement_blocks.join(
                    blocks_found,
                    blocks_found.c.blockhash == settlement_blocks.c.blockhash,
                )
            )
            .where(settlement_blocks.c.settlement_id.in_(settlement_ids))
            .order_by(
                settlement_blocks.c.settlement_id.asc(),
                blocks_found.c.found_at.asc(),
                settlement_blocks.c.id.asc()
            )
        )
        
        result: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            settlement_id = int(row["settlement_id"])
            result.setdefault(settlement_id, []).append(row)
        
        return result

    def list_matured_blocks_in_window(
        self,
        matured_start: datetime,
        matured_end: datetime,
    ) -> list[dict[str, Any]]:
        """Return blocks_found rows in [matured_start, matured_end) not yet linked to a settlement."""
        _require_tzaware("matured_start", matured_start)
        _require_tzaware("matured_end", matured_end)
        from sqlalchemy import not_, exists
        stmt = (
            select(
                blocks_found.c.id,
                blocks_found.c.blockhash,
                blocks_found.c.found_at,
                blocks_found.c.channel_id,
                blocks_found.c.worker_identity,
                blocks_found.c.source,
            )
            .where(
                blocks_found.c.found_at >= matured_start,
                blocks_found.c.found_at < matured_end,
                not_(
                    exists(
                        select(settlement_blocks.c.blockhash).where(
                            settlement_blocks.c.blockhash == blocks_found.c.blockhash
                        )
                    )
                ),
            )
            .order_by(blocks_found.c.found_at.asc(), blocks_found.c.id.asc())
        )
        return self._select_all(stmt)

    def list_retry_blocks(
        self,
        matured_end: datetime,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Return blocks already linked to a settlement but with no resolved reward (for retry)."""
        _require_tzaware("matured_end", matured_end)
        from sqlalchemy import or_
        stmt = (
            select(
                blocks_found.c.id,
                blocks_found.c.blockhash,
                blocks_found.c.found_at,
                blocks_found.c.channel_id,
                blocks_found.c.worker_identity,
                blocks_found.c.source,
                settlement_blocks.c.settlement_id,
            )
            .select_from(
                blocks_found.join(
                    settlement_blocks,
                    settlement_blocks.c.blockhash == blocks_found.c.blockhash,
                )
            )
            .where(
                blocks_found.c.found_at < matured_end,
                or_(
                    settlement_blocks.c.reward_sats.is_(None),
                    settlement_blocks.c.reward_sats <= 0,
                ),
            )
            .order_by(blocks_found.c.found_at.asc(), blocks_found.c.id.asc())
            .limit(limit)
        )
        return self._select_all(stmt)

    def bulk_link_settlement_blocks(
        self,
        settlement_id: int,
        blocks: list[dict[str, Any]],
    ) -> int:
        """Link a list of block dicts (each with 'blockhash' and 'reward_sats') to a settlement.

        Ignores conflicts (a block already linked to another settlement is silently skipped).
        Returns the number of rows successfully inserted.
        """
        inserted = 0
        for block in blocks:
            blockhash = str(block["blockhash"])
            reward_sats = int(block.get("reward_sats") or 0)
            try:
                self.link_settlement_block(
                    settlement_id=settlement_id,
                    blockhash=blockhash,
                    reward_sats=reward_sats,
                )
                inserted += 1
            except (ValueError, RuntimeError):
                # Already linked to same or different settlement — skip silently
                pass
        return inserted

    def list_settlement_windows_paginated(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        return self._select_all(
            select(settlement_windows)
            .order_by(settlement_windows.c.work_window_end.desc(), settlement_windows.c.id.desc())
            .limit(limit)
            .offset(offset)
        )

    def list_settlement_history(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._select_all(
            select(settlement_windows)
            .order_by(settlement_windows.c.work_window_end.desc(), settlement_windows.c.id.desc())
            .limit(limit)
        )
        for row in rows:
            settlement_id = int(row["id"])
            row["user_credits"] = self.list_settlement_user_credits_with_users(settlement_id)
            row["user_work"] = self.list_settlement_user_work_with_users(settlement_id)
            row["settlement_blocks"] = self.list_settlement_blocks(settlement_id)
            summary = self.get_summary_snapshot_by_settlement_id(settlement_id)
            row["summary_snapshot"] = summary
            row["summary_snapshot_miners"] = (
                self.list_summary_snapshot_miners(int(summary["id"]))
                if summary is not None
                else []
            )
        return rows

    def create_account_ledger_entry(
        self,
        *,
        user_id: int,
        entry_type: str,
        amount_sats: int,
        direction: str,
        settlement_credit_id: int | None = None,
        memo: str | None = None,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        _require_tzaware("created_at", created_at)
        values = _clean_values(
            {
                "user_id": user_id,
                "entry_type": entry_type,
                "amount_sats": amount_sats,
                "direction": direction,
                "settlement_credit_id": settlement_credit_id,
                "memo": memo,
                "created_at": created_at,
            }
        )
        statement = account_ledger_entries.insert().values(**values).returning(*account_ledger_entries.c)
        return self._execute_returning_one(statement)

    def get_account_ledger_entry(self, entry_id: int) -> dict[str, Any] | None:
        return self._select_one_or_none(
            select(account_ledger_entries).where(account_ledger_entries.c.id == entry_id)
        )

    def get_account_ledger_entry_by_settlement_credit_id(
        self,
        settlement_credit_id: int,
    ) -> dict[str, Any] | None:
        return self._select_one_or_none(
            select(account_ledger_entries).where(
                account_ledger_entries.c.settlement_credit_id == settlement_credit_id
            )
        )

    def set_account_balance(
        self,
        *,
        user_id: int,
        balance_sats: int,
        updated_at: datetime | None = None,
    ) -> dict[str, Any]:
        _require_tzaware("updated_at", updated_at)
        values = _clean_values(
            {
                "user_id": user_id,
                "balance_sats": balance_sats,
                "updated_at": updated_at or utcnow(),
            }
        )
        statement = (
            pg_insert(account_balances)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[account_balances.c.user_id],
                set_={
                    "balance_sats": balance_sats,
                    "updated_at": values["updated_at"],
                },
            )
            .returning(*account_balances.c)
        )
        return self._execute_returning_one(statement)

    def get_account_balance(self, user_id: int) -> dict[str, Any] | None:
        return self._select_one_or_none(
            select(account_balances).where(account_balances.c.user_id == user_id)
        )

    def create_audit_event(
        self,
        *,
        event_type: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        payload: dict[str, Any] | list[Any] | None = None,
        payload_hash: str | None = None,
        previous_hash: str | None = None,
        event_hash: str | None = None,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        _require_tzaware("created_at", created_at)
        values = _clean_values(
            {
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "payload": payload,
                "payload_hash": payload_hash,
                "previous_hash": previous_hash,
                "event_hash": event_hash,
                "created_at": created_at,
            }
        )
        statement = audit_events.insert().values(**values).returning(*audit_events.c)
        return self._execute_returning_one(statement)

    def get_audit_event(self, event_id: int) -> dict[str, Any] | None:
        return self._select_one_or_none(select(audit_events).where(audit_events.c.id == event_id))

    def upsert_service_cursor(
        self,
        *,
        cursor_name: str,
        cursor_value: str | None,
        updated_at: datetime | None = None,
    ) -> dict[str, Any]:
        _require_tzaware("updated_at", updated_at)
        effective_updated_at = updated_at or utcnow()
        statement = (
            pg_insert(service_cursors)
            .values(
                cursor_name=cursor_name,
                cursor_value=cursor_value,
                updated_at=effective_updated_at,
            )
            .on_conflict_do_update(
                index_elements=[service_cursors.c.cursor_name],
                set_={
                    "cursor_value": cursor_value,
                    "updated_at": effective_updated_at,
                },
            )
            .returning(*service_cursors.c)
        )
        return self._execute_returning_one(statement)

    def get_service_cursor(self, cursor_name: str) -> dict[str, Any] | None:
        return self._select_one_or_none(
            select(service_cursors).where(service_cursors.c.cursor_name == cursor_name)
        )

    def upsert_carry_state(
        self,
        *,
        bucket: str = "default",
        carry_btc: Decimal | int | str,
        updated_at: datetime | None = None,
    ) -> dict[str, Any]:
        _require_tzaware("updated_at", updated_at)
        effective_updated_at = updated_at or utcnow()
        statement = (
            pg_insert(carry_state)
            .values(
                bucket=bucket,
                carry_btc=_as_decimal(carry_btc),
                updated_at=effective_updated_at,
            )
            .on_conflict_do_update(
                index_elements=[carry_state.c.bucket],
                set_={
                    "carry_btc": _as_decimal(carry_btc),
                    "updated_at": effective_updated_at,
                },
            )
            .returning(*carry_state.c)
        )
        return self._execute_returning_one(statement)

    def get_carry_state(self, *, bucket: str = "default") -> dict[str, Any] | None:
        return self._select_one_or_none(select(carry_state).where(carry_state.c.bucket == bucket))

    def upsert_work_accrual_bucket(
        self,
        *,
        user_id: int,
        accumulated_work: Decimal | int | str,
        updated_at: datetime | None = None,
    ) -> dict[str, Any]:
        _require_tzaware("updated_at", updated_at)
        effective_updated_at = updated_at or utcnow()
        statement = (
            pg_insert(work_accrual_bucket)
            .values(
                user_id=user_id,
                accumulated_work=_as_decimal(accumulated_work),
                updated_at=effective_updated_at,
            )
            .on_conflict_do_update(
                index_elements=[work_accrual_bucket.c.user_id],
                set_={
                    "accumulated_work": _as_decimal(accumulated_work),
                    "updated_at": effective_updated_at,
                },
            )
            .returning(*work_accrual_bucket.c)
        )
        return self._execute_returning_one(statement)

    def get_work_accrual_bucket(self, user_id: int) -> dict[str, Any] | None:
        return self._select_one_or_none(
            select(work_accrual_bucket).where(work_accrual_bucket.c.user_id == user_id)
        )

    def list_all_work_accrual_buckets(self) -> list[dict[str, Any]]:
        return self._select_all(select(work_accrual_bucket).order_by(work_accrual_bucket.c.user_id.asc()))

    def create_payout_event(
        self,
        *,
        settlement_credit_id: int,
        payload_json: str,
        status: str = "pending",
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        _require_tzaware("created_at", created_at)
        values = _clean_values(
            {
                "settlement_credit_id": settlement_credit_id,
                "payload_json": payload_json,
                "status": status,
                "created_at": created_at,
            }
        )
        statement = payout_events.insert().values(**values).returning(*payout_events.c)
        return self._execute_returning_one(statement)

    def get_payout_event_by_settlement_credit_id(self, settlement_credit_id: int) -> dict[str, Any] | None:
        return self._select_one_or_none(
            select(payout_events).where(payout_events.c.settlement_credit_id == settlement_credit_id)
        )

    def update_payout_event_status(
        self,
        settlement_credit_id: int,
        status: str,
    ) -> dict[str, Any] | None:
        statement = (
            payout_events
            .update()
            .where(payout_events.c.settlement_credit_id == settlement_credit_id)
            .values(status=status)
            .returning(*payout_events.c)
        )
        return self._execute_returning_one(statement)

    def list_pending_payout_events(self) -> list[dict[str, Any]]:
        return self._select_all(
            select(
                payout_events.c.id,
                payout_events.c.settlement_credit_id,
                payout_events.c.payload_json,
                payout_events.c.status,
                payout_events.c.created_at,
                settlement_user_credits.c.user_id,
                settlement_user_credits.c.settlement_id,
                settlement_user_credits.c.amount_sats,
                settlement_user_credits.c.idempotency_key,
                users.c.username,
                settlement_windows.c.work_window_start,
                settlement_windows.c.work_window_end,
            )
            .select_from(
                payout_events
                .join(
                    settlement_user_credits,
                    settlement_user_credits.c.id == payout_events.c.settlement_credit_id,
                )
                .join(
                    users,
                    users.c.id == settlement_user_credits.c.user_id,
                )
                .join(
                    settlement_windows,
                    settlement_windows.c.id == settlement_user_credits.c.settlement_id,
                )
            )
            .where(payout_events.c.status != "sent")
            .order_by(payout_events.c.id.asc())
        )

    def update_settlement_credit_status(
        self,
        settlement_credit_id: int,
        status: str,
    ) -> dict[str, Any] | None:
        statement = (
            settlement_user_credits
            .update()
            .where(settlement_user_credits.c.id == settlement_credit_id)
            .values(status=status)
            .returning(*settlement_user_credits.c)
        )
        return self._execute_returning_one(statement)
