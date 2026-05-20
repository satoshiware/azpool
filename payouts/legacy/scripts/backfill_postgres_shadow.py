from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
import sys
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_settings
from app.db import make_engine, make_session_factory
from app.mapping import parse_identity
from app.models import Settlement, SnapshotBlock, UserPayout
from app.postgres_db import make_postgres_engine, make_postgres_session_factory
from app.postgres_repositories import PostgresLedgerRepository
from app.postgres_shadow_compare import (
    SQLiteSettlementContext,
    as_utc_aware,
    btc_to_sats,
    compare_postgres_shadow_settlement,
    get_postgres_shadow_compare_repository,
    load_sqlite_settlement_context,
    normalize_work,
    uses_work_basis_for_shadow_write,
)


ZERO = Decimal("0")


@dataclass(frozen=True)
class ExpectedIdentityRow:
    username: str
    identity: str
    worker_name: str | None


@dataclass(frozen=True)
class ExpectedUserSettlementRow:
    username: str
    share_delta: int
    work_delta: Decimal
    payout_fraction: Decimal
    credit_amount_sats: int | None
    credit_status: str | None
    idempotency_key: str | None


@dataclass(frozen=True)
class ExpectedBlockRow:
    blockhash: str
    found_at: datetime
    channel_id: int | None
    worker_identity: str | None
    source: str
    reward_sats: int | None
    reward_fetched_at: datetime | None


@dataclass(frozen=True)
class SettlementBackfillPlan:
    settlement_id: int
    settlement_status: str
    work_window_start: datetime
    work_window_end: datetime
    settlement_run_at: datetime
    maturity_offset_minutes: int
    total_reward_sats: int
    total_work: Decimal
    total_shares: int
    completed_at: datetime | None
    user_rows: list[ExpectedUserSettlementRow]
    identity_rows: list[ExpectedIdentityRow]
    block_rows: list[ExpectedBlockRow]


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _result(
    settlement_id: int,
    status: str,
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "settlement_id": settlement_id,
        "status": status,
    }
    payload.update(extra)
    return payload


def _settlement_ids_to_process(
    session,
    *,
    settlement_id: int | None,
    start_id: int | None,
    end_id: int | None,
    limit: int | None,
) -> list[int]:
    statement = select(Settlement.id).order_by(Settlement.id.asc())
    if settlement_id is not None:
        statement = statement.where(Settlement.id == settlement_id)
    else:
        if start_id is not None:
            statement = statement.where(Settlement.id >= start_id)
        if end_id is not None:
            statement = statement.where(Settlement.id <= end_id)
        if limit is not None:
            statement = statement.limit(limit)
    return [int(row[0]) for row in session.execute(statement).all()]


def _build_plan(context: SQLiteSettlementContext) -> SettlementBackfillPlan:
    settlement = context.settlement
    settlement_run_at = as_utc_aware(settlement.period_end)
    work_window_start = as_utc_aware(context.work_window_start)
    work_window_end = as_utc_aware(context.work_window_end)
    use_work_basis = uses_work_basis_for_shadow_write(context)
    maturity_offset_minutes = max(
        0,
        int((settlement.period_end - context.work_window_end).total_seconds() // 60),
    )

    payouts_by_username = {user.username: payout for payout, user in context.payout_rows}
    usernames = sorted(set(context.user_contributions) | set(payouts_by_username))
    user_rows: list[ExpectedUserSettlementRow] = []
    for username in usernames:
        contribution = context.user_contributions.get(username)
        payout = payouts_by_username.get(username)
        share_delta = int(getattr(contribution, "share_delta", 0) or 0)
        work_delta = Decimal(str(getattr(contribution, "work_delta", ZERO) or ZERO))
        payout_fraction = Decimal("0")
        credit_amount_sats: int | None = None
        credit_status: str | None = None
        idempotency_key: str | None = None

        if payout is not None:
            payout_fraction = Decimal(str(payout.payout_fraction or 0))
            credit_amount_sats = btc_to_sats(payout.amount_btc)
            credit_status = payout.status
            idempotency_key = payout.idempotency_key
            if use_work_basis:
                work_delta = Decimal(str(payout.contribution_value or 0))

        user_rows.append(
            ExpectedUserSettlementRow(
                username=username,
                share_delta=share_delta,
                work_delta=normalize_work(work_delta),
                payout_fraction=payout_fraction,
                credit_amount_sats=credit_amount_sats,
                credit_status=credit_status,
                idempotency_key=idempotency_key,
            )
        )

    identity_index: dict[str, ExpectedIdentityRow] = {}
    for identity in context.identity_deltas:
        try:
            parsed = parse_identity(identity)
        except ValueError:
            continue
        identity_index[identity] = ExpectedIdentityRow(
            username=parsed.username,
            identity=identity,
            worker_name=parsed.worker,
        )
    for block_row in context.block_rows:
        if not block_row.worker_identity:
            continue
        try:
            parsed = parse_identity(block_row.worker_identity)
        except ValueError:
            continue
        identity_index[block_row.worker_identity] = ExpectedIdentityRow(
            username=parsed.username,
            identity=block_row.worker_identity,
            worker_name=parsed.worker,
        )

    block_rows = [
        ExpectedBlockRow(
            blockhash=row.blockhash,
            found_at=as_utc_aware(row.found_at),
            channel_id=int(row.channel_id) if row.channel_id is not None else None,
            worker_identity=row.worker_identity,
            source=row.source,
            reward_sats=int(row.reward_sats) if row.reward_sats is not None else None,
            reward_fetched_at=as_utc_aware(row.reward_fetched_at),
        )
        for row in context.block_rows
    ]

    return SettlementBackfillPlan(
        settlement_id=int(settlement.id),
        settlement_status=settlement.status,
        work_window_start=work_window_start,
        work_window_end=work_window_end,
        settlement_run_at=settlement_run_at,
        maturity_offset_minutes=maturity_offset_minutes,
        total_reward_sats=btc_to_sats(settlement.pool_reward_btc),
        total_work=normalize_work(settlement.total_work or 0),
        total_shares=int(settlement.total_shares or 0),
        completed_at=settlement_run_at if settlement.status == "completed" else None,
        user_rows=user_rows,
        identity_rows=sorted(identity_index.values(), key=lambda row: row.identity),
        block_rows=block_rows,
    )


def _validate_plan(context: SQLiteSettlementContext, plan: SettlementBackfillPlan) -> list[str]:
    reasons: list[str] = []
    if plan.work_window_end < plan.work_window_start:
        reasons.append("work_window_end precedes work_window_start")

    positive_reward_rows = [row for row in plan.block_rows if row.reward_sats is not None and row.reward_sats > 0]
    rewarded_total = sum(int(row.reward_sats or 0) for row in positive_reward_rows)
    if positive_reward_rows and rewarded_total != plan.total_reward_sats:
        reasons.append(
            "rewarded SnapshotBlock total does not match SQLite settlement reward total"
        )
    if plan.total_reward_sats > 0 and context.block_rows and rewarded_total == 0:
        reasons.append(
            "linked settlement blocks exist but none carry positive reward_sats for a rewarded settlement"
        )

    blockhashes = [row.blockhash for row in plan.block_rows]
    if len(blockhashes) != len(set(blockhashes)):
        reasons.append("duplicate blockhash rows are linked to the SQLite settlement")

    payout_keys = [row.idempotency_key for row in plan.user_rows if row.idempotency_key]
    if len(payout_keys) != len(set(payout_keys)):
        reasons.append("duplicate payout idempotency keys were found in SQLite")

    return reasons


def _inspect_existing_rows(
    repository: PostgresLedgerRepository,
    plan: SettlementBackfillPlan,
) -> dict[str, Any]:
    forceable_conflicts: list[dict[str, Any]] = []
    unsafe_conflicts: list[dict[str, Any]] = []
    missing_components: list[str] = []

    settlement_row = repository.get_settlement_window_by_range(
        work_window_start=plan.work_window_start,
        work_window_end=plan.work_window_end,
    )
    if settlement_row is None:
        missing_components.append("settlement_window")
    else:
        expected_pairs = {
            "status": plan.settlement_status,
            "settlement_run_at": plan.settlement_run_at,
            "maturity_offset_minutes": plan.maturity_offset_minutes,
            "total_reward_sats": plan.total_reward_sats,
            "total_work": plan.total_work,
            "total_shares": plan.total_shares,
            "completed_at": plan.completed_at,
        }
        for field, expected in expected_pairs.items():
            actual = settlement_row.get(field)
            if field in {"settlement_run_at", "completed_at"}:
                actual = as_utc_aware(actual)
            if field == "total_work":
                actual = normalize_work(actual or 0)
            if actual != expected:
                forceable_conflicts.append(
                    {
                        "component": "settlement_window",
                        "field": field,
                        "expected": expected,
                        "actual": actual,
                    }
                )

    shadow_users: dict[str, dict[str, Any] | None] = {}
    for row in plan.user_rows:
        shadow_user = repository.get_user_by_username(row.username)
        shadow_users[row.username] = shadow_user
        if shadow_user is None:
            missing_components.append(f"user:{row.username}")

    for identity_row in plan.identity_rows:
        existing = repository.get_miner_identity(identity_row.identity)
        target_user = shadow_users.get(identity_row.username)
        if existing is None:
            missing_components.append(f"miner_identity:{identity_row.identity}")
            continue
        if target_user is not None and int(existing["user_id"]) != int(target_user["id"]):
            forceable_conflicts.append(
                {
                    "component": "miner_identity",
                    "identity": identity_row.identity,
                    "field": "user_id",
                    "expected": int(target_user["id"]),
                    "actual": int(existing["user_id"]),
                }
            )
        if existing.get("worker_name") != identity_row.worker_name:
            forceable_conflicts.append(
                {
                    "component": "miner_identity",
                    "identity": identity_row.identity,
                    "field": "worker_name",
                    "expected": identity_row.worker_name,
                    "actual": existing.get("worker_name"),
                }
            )

    shadow_settlement_id = int(settlement_row["id"]) if settlement_row is not None else None
    if shadow_settlement_id is not None:
        for row in plan.user_rows:
            shadow_user = shadow_users.get(row.username)
            if shadow_user is None:
                continue
            user_id = int(shadow_user["id"])
            user_work = repository.get_settlement_user_work(
                settlement_id=shadow_settlement_id,
                user_id=user_id,
            )
            if user_work is None:
                missing_components.append(f"user_work:{row.username}")
            else:
                if int(user_work["share_delta"] or 0) != row.share_delta:
                    forceable_conflicts.append(
                        {
                            "component": "settlement_user_work",
                            "username": row.username,
                            "field": "share_delta",
                            "expected": row.share_delta,
                            "actual": int(user_work["share_delta"] or 0),
                        }
                    )
                actual_work = normalize_work(user_work["work_delta"] or 0)
                if actual_work != normalize_work(row.work_delta):
                    forceable_conflicts.append(
                        {
                            "component": "settlement_user_work",
                            "username": row.username,
                            "field": "work_delta",
                            "expected": normalize_work(row.work_delta),
                            "actual": actual_work,
                        }
                    )
                actual_fraction = Decimal(str(user_work["payout_fraction"] or 0))
                if actual_fraction != row.payout_fraction:
                    forceable_conflicts.append(
                        {
                            "component": "settlement_user_work",
                            "username": row.username,
                            "field": "payout_fraction",
                            "expected": row.payout_fraction,
                            "actual": actual_fraction,
                        }
                    )

            if row.credit_amount_sats is None:
                continue
            credit = repository.get_settlement_user_credit(
                settlement_id=shadow_settlement_id,
                user_id=user_id,
            )
            if credit is None:
                missing_components.append(f"user_credit:{row.username}")
            else:
                if int(credit["amount_sats"] or 0) != row.credit_amount_sats:
                    forceable_conflicts.append(
                        {
                            "component": "settlement_user_credit",
                            "username": row.username,
                            "field": "amount_sats",
                            "expected": row.credit_amount_sats,
                            "actual": int(credit["amount_sats"] or 0),
                        }
                    )
                if credit.get("status") != row.credit_status:
                    forceable_conflicts.append(
                        {
                            "component": "settlement_user_credit",
                            "username": row.username,
                            "field": "status",
                            "expected": row.credit_status,
                            "actual": credit.get("status"),
                        }
                    )
                if credit.get("idempotency_key") != row.idempotency_key:
                    forceable_conflicts.append(
                        {
                            "component": "settlement_user_credit",
                            "username": row.username,
                            "field": "idempotency_key",
                            "expected": row.idempotency_key,
                            "actual": credit.get("idempotency_key"),
                        }
                    )

    for block_row in plan.block_rows:
        existing_block = repository.get_block_found(block_row.blockhash)
        if existing_block is None:
            missing_components.append(f"block_found:{block_row.blockhash}")
        else:
            expected_block_fields = {
                "found_at": block_row.found_at,
                "channel_id": block_row.channel_id,
                "worker_identity": block_row.worker_identity,
                "source": block_row.source,
            }
            for field, expected in expected_block_fields.items():
                actual = existing_block.get(field)
                if field == "found_at":
                    actual = as_utc_aware(actual)
                if actual != expected:
                    forceable_conflicts.append(
                        {
                            "component": "block_found",
                            "blockhash": block_row.blockhash,
                            "field": field,
                            "expected": expected,
                            "actual": actual,
                        }
                    )

        if block_row.reward_sats is None or block_row.reward_sats <= 0:
            continue

        existing_reward = repository.get_block_reward(block_row.blockhash)
        if existing_reward is None:
            missing_components.append(f"block_reward:{block_row.blockhash}")
        else:
            actual_reward = int(existing_reward["reward_sats"] or 0)
            if actual_reward != block_row.reward_sats:
                forceable_conflicts.append(
                    {
                        "component": "block_reward",
                        "blockhash": block_row.blockhash,
                        "field": "reward_sats",
                        "expected": block_row.reward_sats,
                        "actual": actual_reward,
                    }
                )

        existing_link = repository.get_settlement_block(block_row.blockhash)
        if existing_link is None:
            missing_components.append(f"settlement_block:{block_row.blockhash}")
            continue
        if shadow_settlement_id is None:
            unsafe_conflicts.append(
                {
                    "component": "settlement_block",
                    "blockhash": block_row.blockhash,
                    "field": "shadow_settlement_missing",
                    "expected": "missing settlement_window blocks cannot be safely verified",
                    "actual": int(existing_link["settlement_id"]),
                }
            )
            continue
        if int(existing_link["settlement_id"]) != shadow_settlement_id:
            unsafe_conflicts.append(
                {
                    "component": "settlement_block",
                    "blockhash": block_row.blockhash,
                    "field": "settlement_id",
                    "expected": shadow_settlement_id,
                    "actual": int(existing_link["settlement_id"]),
                }
            )
        if int(existing_link["reward_sats"] or 0) != block_row.reward_sats:
            unsafe_conflicts.append(
                {
                    "component": "settlement_block",
                    "blockhash": block_row.blockhash,
                    "field": "reward_sats",
                    "expected": block_row.reward_sats,
                    "actual": int(existing_link["reward_sats"] or 0),
                }
            )

    return {
        "settlement_row": settlement_row,
        "shadow_users": shadow_users,
        "missing_components": sorted(set(missing_components)),
        "forceable_conflicts": forceable_conflicts,
        "unsafe_conflicts": unsafe_conflicts,
    }


def _apply_backfill(
    repository: PostgresLedgerRepository,
    plan: SettlementBackfillPlan,
) -> dict[str, Any]:
    shadow_settlement = repository.upsert_settlement_window(
        settlement_run_at=plan.settlement_run_at,
        work_window_start=plan.work_window_start,
        work_window_end=plan.work_window_end,
        maturity_offset_minutes=plan.maturity_offset_minutes,
        status=plan.settlement_status,
        total_reward_sats=plan.total_reward_sats,
        total_work=plan.total_work,
        total_shares=plan.total_shares,
        completed_at=plan.completed_at,
    )

    shadow_users: dict[str, dict[str, Any]] = {}
    for row in plan.user_rows:
        shadow_users[row.username] = repository.upsert_user(row.username)

    for identity_row in plan.identity_rows:
        shadow_user = shadow_users[identity_row.username]
        repository.upsert_miner_identity(
            user_id=int(shadow_user["id"]),
            identity=identity_row.identity,
            worker_name=identity_row.worker_name,
        )

    for block_row in plan.block_rows:
        repository.upsert_block_found(
            blockhash=block_row.blockhash,
            found_at=block_row.found_at,
            channel_id=block_row.channel_id,
            worker_identity=block_row.worker_identity,
            source=block_row.source,
        )
        if block_row.reward_sats is None or block_row.reward_sats <= 0:
            continue
        repository.upsert_block_reward(
            blockhash=block_row.blockhash,
            reward_sats=block_row.reward_sats,
            fetched_at=block_row.reward_fetched_at or plan.settlement_run_at,
        )
        repository.link_settlement_block(
            settlement_id=int(shadow_settlement["id"]),
            blockhash=block_row.blockhash,
            reward_sats=block_row.reward_sats,
        )

    for row in plan.user_rows:
        shadow_user = shadow_users[row.username]
        repository.upsert_settlement_user_work(
            settlement_id=int(shadow_settlement["id"]),
            user_id=int(shadow_user["id"]),
            share_delta=row.share_delta,
            work_delta=row.work_delta,
            payout_fraction=row.payout_fraction,
        )
        if row.credit_amount_sats is None:
            continue
        repository.upsert_settlement_user_credit(
            settlement_id=int(shadow_settlement["id"]),
            user_id=int(shadow_user["id"]),
            amount_sats=row.credit_amount_sats,
            idempotency_key=str(row.idempotency_key),
            status=str(row.credit_status or "pending"),
        )

    return {
        "shadow_settlement_id": int(shadow_settlement["id"]),
        "user_count": len(plan.user_rows),
        "identity_count": len(plan.identity_rows),
        "block_count": len(plan.block_rows),
        "rewarded_block_count": len(
            [row for row in plan.block_rows if row.reward_sats is not None and row.reward_sats > 0]
        ),
        "credit_count": len([row for row in plan.user_rows if row.credit_amount_sats is not None]),
    }


def _verify(
    session,
    settlement_id: int,
    repository: PostgresLedgerRepository,
) -> dict[str, Any]:
    comparison, status_code = compare_postgres_shadow_settlement(
        session,
        settlement_id,
        repository=repository,
    )
    return {
        "comparison_status": comparison.get("comparison_status"),
        "comparison_http_status": status_code,
        "comparison_mismatches": comparison.get("mismatches", []),
        "comparison_error": comparison.get("error"),
    }


def backfill_postgres_shadow(
    session,
    repository: PostgresLedgerRepository,
    *,
    settlement_id: int | None = None,
    start_id: int | None = None,
    end_id: int | None = None,
    limit: int | None = None,
    write: bool = False,
    force: bool = False,
    verbose: bool = False,
    include_non_completed: bool = False,
    verify: bool = True,
) -> dict[str, Any]:
    candidate_ids = _settlement_ids_to_process(
        session,
        settlement_id=settlement_id,
        start_id=start_id,
        end_id=end_id,
        limit=limit,
    )
    summary: dict[str, Any] = {
        "mode": "write" if write else "dry_run",
        "total_considered": len(candidate_ids),
        "dry_run_count": 0,
        "inserted_count": 0,
        "already_present_count": 0,
        "matched_count": 0,
        "mismatched_count": 0,
        "skipped_count": 0,
        "error_count": 0,
        "settlement_results": [],
    }

    for current_settlement_id in candidate_ids:
        if verbose:
            print(
                f"[{summary['mode']}] inspecting settlement {current_settlement_id}",
                file=sys.stderr,
            )

        context = load_sqlite_settlement_context(session, current_settlement_id)
        if context is None:
            result = _result(
                current_settlement_id,
                "error",
                reason="SQLite settlement was not found",
            )
            summary["error_count"] += 1
            summary["settlement_results"].append(result)
            continue

        if context.settlement.status != "completed" and not include_non_completed:
            result = _result(
                current_settlement_id,
                "skipped",
                reason=f"SQLite settlement status {context.settlement.status!r} is not eligible without --include-non-completed",
            )
            summary["skipped_count"] += 1
            summary["settlement_results"].append(result)
            continue

        plan = _build_plan(context)
        validation_errors = _validate_plan(context, plan)
        if validation_errors:
            result = _result(
                current_settlement_id,
                "skipped",
                reason="; ".join(validation_errors),
            )
            summary["skipped_count"] += 1
            summary["settlement_results"].append(result)
            continue

        inspection = _inspect_existing_rows(repository, plan)
        forceable_conflicts = inspection["forceable_conflicts"]
        unsafe_conflicts = inspection["unsafe_conflicts"]
        missing_components = inspection["missing_components"]

        if unsafe_conflicts:
            verification = _verify(session, current_settlement_id, repository) if verify else {}
            result = _result(
                current_settlement_id,
                "mismatched",
                reason="unsafe existing Postgres conflicts prevent backfill",
                missing_components=missing_components,
                conflicts=unsafe_conflicts,
                forceable_conflicts=forceable_conflicts,
                **verification,
            )
            summary["mismatched_count"] += 1
            if verification.get("comparison_status") == "matched":
                summary["matched_count"] += 1
            summary["settlement_results"].append(result)
            continue

        has_forceable_conflicts = bool(forceable_conflicts)
        has_missing = bool(missing_components)

        if not has_missing and not has_forceable_conflicts:
            verification = _verify(session, current_settlement_id, repository) if verify else {}
            result = _result(
                current_settlement_id,
                "already_present",
                missing_components=[],
                forceable_conflicts=[],
                **verification,
            )
            summary["already_present_count"] += 1
            if verification.get("comparison_status") == "matched":
                summary["matched_count"] += 1
            elif verification.get("comparison_status") == "mismatched":
                summary["mismatched_count"] += 1
            summary["settlement_results"].append(result)
            continue

        if has_forceable_conflicts and not force:
            verification = _verify(session, current_settlement_id, repository) if verify else {}
            result = _result(
                current_settlement_id,
                "mismatched",
                reason="existing Postgres rows conflict; rerun with --force to upsert forceable rows",
                missing_components=missing_components,
                forceable_conflicts=forceable_conflicts,
                **verification,
            )
            summary["mismatched_count"] += 1
            if verification.get("comparison_status") == "matched":
                summary["matched_count"] += 1
            summary["settlement_results"].append(result)
            continue

        if not write:
            result = _result(
                current_settlement_id,
                "dry_run",
                missing_components=missing_components,
                forceable_conflicts=forceable_conflicts,
                would_force=bool(force and has_forceable_conflicts),
            )
            summary["dry_run_count"] += 1
            summary["settlement_results"].append(result)
            continue

        try:
            applied = _apply_backfill(repository, plan)
        except Exception as exc:
            result = _result(
                current_settlement_id,
                "error",
                reason=f"write failed: {exc}",
                missing_components=missing_components,
                forceable_conflicts=forceable_conflicts,
            )
            summary["error_count"] += 1
            summary["settlement_results"].append(result)
            continue

        verification = _verify(session, current_settlement_id, repository) if verify else {}
        result = _result(
            current_settlement_id,
            "inserted",
            write_performed=True,
            forced=bool(force and has_forceable_conflicts),
            missing_components=missing_components,
            forceable_conflicts=forceable_conflicts,
            **applied,
            **verification,
        )
        summary["inserted_count"] += 1
        if verification.get("comparison_status") == "matched":
            summary["matched_count"] += 1
        elif verification.get("comparison_status") == "mismatched":
            summary["mismatched_count"] += 1
        summary["settlement_results"].append(result)

    return summary


def _require_postgres_repository() -> PostgresLedgerRepository:
    database_url = os.getenv("POSTGRES_LEDGER_DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("POSTGRES_LEDGER_DATABASE_URL must be set for backfill")

    try:
        engine = make_postgres_engine(database_url)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return get_postgres_shadow_compare_repository()
    except SQLAlchemyError as exc:
        raise RuntimeError(f"Failed to connect to Postgres: {exc}") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill historical SQLite settlements into Postgres shadow tables.",
    )
    parser.add_argument("--settlement-id", type=int, default=None)
    parser.add_argument("--start-id", type=int, default=None)
    parser.add_argument("--end-id", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Explicit dry-run mode. This is the default.")
    parser.add_argument("--write", action="store_true", help="Perform writes to Postgres shadow tables.")
    parser.add_argument("--force", action="store_true", help="Allow forceable upsert conflicts to be updated.")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--include-non-completed",
        action="store_true",
        help="Include non-completed SQLite settlements. Default behavior skips them safely.",
    )
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit must be positive")
    if args.start_id is not None and args.end_id is not None and args.start_id > args.end_id:
        raise SystemExit("--start-id cannot be greater than --end-id")
    if args.write and args.dry_run:
        raise SystemExit("Use either --dry-run or --write, not both")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_args(args)

    settings = load_settings()
    sqlite_engine = make_engine(settings.db_path)
    sqlite_session_factory = make_session_factory(sqlite_engine)
    repository = _require_postgres_repository()

    with sqlite_session_factory() as session:
        summary = backfill_postgres_shadow(
            session,
            repository,
            settlement_id=args.settlement_id,
            start_id=args.start_id,
            end_id=args.end_id,
            limit=args.limit,
            write=bool(args.write),
            force=bool(args.force),
            verbose=bool(args.verbose),
            include_non_completed=bool(args.include_non_completed),
            verify=True,
        )

    print(json.dumps(summary, indent=2, default=_json_default))

    if summary["error_count"] > 0:
        return 1
    if args.write and summary["mismatched_count"] > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
