from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
import traceback
from typing import Any
import uuid

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from app.audit import (
    build_payout_audit_event,
    read_recent_audit_entries,
    rotate_payout_audit_log,
    write_payout_audit_log,
)
from app.config import (
    POSTGRES_LEDGER_READ_MODE_AUTHORITATIVE,
    POSTGRES_LEDGER_READ_MODE_SHADOW_CANDIDATE,
    load_settings,
)
from app.db import make_engine, make_session_factory
from app.delta import compute_user_contribution_deltas
from app.hooks import (
    run_block_event_replay_hook,
    run_reward_refetch_hook,
    run_settlement_replay_hook,
    run_startup_reconciliation_hook,
)
from app.init_db import init_db
from app.mapping import parse_identity
from app.models import BlockCounterState, PayoutEvent, Settlement, SnapshotBlock, User, UserPayout
from app.poller import poll_channels_once_with_blocks, poll_metrics_once, upsert_snapshot_blocks, upsert_blocks_found_postgres
from app.pool_client import PoolApiError, fetch_block_rewards_by_hashes, fetch_blocks_found_in_window
from app.postgres_db import make_postgres_engine, make_postgres_session_factory
from app.postgres_delta import compute_user_contribution_deltas_postgres
from app.postgres_read_payloads import build_latest_settlement_payload, build_service_metrics_payload
from app.postgres_repositories import PostgresLedgerRepository
from app.runtime_cutover import should_fail_closed_on_postgres_primary
from app.postgres_sender import process_payout_events_postgres
from app.postgres_settlement import run_settlement_postgres
from app.postgres_shadow_compare import (
    audit_postgres_shadow_settlements,
    compare_postgres_shadow_settlement,
    get_postgres_shadow_compare_repository,
)
from app.reward_contract import compute_matured_window
from app.scheduler import start_scheduler, stop_scheduler
from app.sender import process_payout_events
from app.settlement import run_settlement

app = FastAPI(title="Mining Payout Service", version="0.1.0")
_SERVICE_STARTED_AT: datetime | None = None
_SATS_PER_BTC = Decimal("100000000")
POSTGRES_READ_ENDPOINT_SETTLEMENT_HISTORY = "settlement_history"
POSTGRES_READ_ENDPOINT_SETTLEMENT_DETAIL = "settlement_detail"
POSTGRES_CANDIDATE_READ_MODES = frozenset(
    {
        POSTGRES_LEDGER_READ_MODE_SHADOW_CANDIDATE,
        POSTGRES_LEDGER_READ_MODE_AUTHORITATIVE,
    }
)


def _sum_payout_amount(payout_rows: list[dict[str, object]]) -> Decimal:
    total = Decimal("0")
    for row in payout_rows:
        total += Decimal(str(row.get("amount_btc") or "0"))
    return total


def _to_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _sats_to_btc_str(value: object) -> str:
    return _to_decimal_str(Decimal(_to_int(value)) / _SATS_PER_BTC)


def _contribution_index(user_contributions: list[dict[str, object]]) -> dict[str, dict[str, Decimal | int]]:
    index: dict[str, dict[str, Decimal | int]] = {}
    for row in user_contributions:
        username = str(row.get("username") or "")
        if not username:
            continue
        index[username] = {
            "share_delta": _to_int(row.get("share_delta")),
            "work_delta": Decimal(str(row.get("work_delta") or "0")),
        }
    return index


def _payout_user_breakdown(
    payout_rows: list[dict[str, object]],
    user_contributions: list[dict[str, object]],
) -> list[dict[str, object]]:
    contributions = _contribution_index(user_contributions)
    breakdown: list[dict[str, object]] = []
    for payout in payout_rows:
        username = str(payout.get("username") or "")
        contrib = contributions.get(username, {"share_delta": 0, "work_delta": Decimal("0")})
        breakdown.append(
            {
                "username": username,
                "amount_btc": _to_decimal_str(payout.get("amount_btc") or "0"),
                "status": payout.get("status"),
                "payout_fraction": str(payout.get("payout_fraction") or "0"),
                "contribution_value": _to_decimal_str(payout.get("contribution_value") or "0"),
                "share_delta": int(contrib["share_delta"]),
                "work_delta": _to_decimal_str(contrib["work_delta"]),
            }
        )
    return breakdown


def _load_block_rows_by_settlement(session: Session, settlement_ids: list[int]) -> dict[int, list[dict[str, object]]]:
    if not settlement_ids:
        return {}

    settings = load_settings()
    if getattr(settings, "postgres_primary_session_enabled", False):
        try:
            postgres_repo = PostgresLedgerRepository(
                make_postgres_session_factory(make_postgres_engine())
            )
            result = postgres_repo.list_settlement_blocks_by_ids(settlement_ids)
            # Convert Postgres rows to match expected format
            converted: dict[int, list[dict[str, object]]] = {}
            for settlement_id, rows in result.items():
                converted[settlement_id] = [
                    {
                        "found_at": row["found_at"].isoformat() if row["found_at"] else "",
                        "channel_id": int(row["channel_id"] or 0),
                        "worker_identity": row["worker_identity"],
                        "blockhash": row["blockhash"],
                        "source": row["source"],
                        "reward_sats": int(row["reward_sats"] or 0),
                        "reward_btc": _to_decimal_str(Decimal(int(row["reward_sats"] or 0)) / Decimal("100000000")),
                    }
                    for row in rows
                ]
            return converted
        except Exception:
            pass  # Fall back to SQLite below
    
    # SQLite fallback
    rows = session.execute(
        select(SnapshotBlock)
        .where(SnapshotBlock.settlement_id.in_(settlement_ids))
        .order_by(
            SnapshotBlock.settlement_id.asc(),
            SnapshotBlock.found_at.asc(),
            SnapshotBlock.id.asc(),
        )
    ).scalars().all()

    grouped: dict[int, list[dict[str, object]]] = {}
    for row in rows:
        settlement_id = int(row.settlement_id or 0)
        if settlement_id <= 0:
            continue
        grouped.setdefault(settlement_id, []).append(
            {
                "found_at": row.found_at.isoformat(),
                "channel_id": int(row.channel_id or 0),
                "worker_identity": row.worker_identity,
                "blockhash": row.blockhash,
                "source": row.source,
                "reward_sats": int(row.reward_sats or 0),
                "reward_btc": _to_decimal_str(Decimal(int(row.reward_sats or 0)) / Decimal("100000000")),
            }
        )

    return grouped


def _compare_with_previous_payout(
    current_user_contributions: list[dict[str, object]],
    previous_user_contributions: list[dict[str, object]],
) -> list[dict[str, object]]:
    current = _contribution_index(current_user_contributions)
    previous = _contribution_index(previous_user_contributions)
    usernames = sorted(set(current.keys()) | set(previous.keys()))
    rows: list[dict[str, object]] = []

    for username in usernames:
        curr_share = int((current.get(username) or {}).get("share_delta", 0))
        curr_work = Decimal(str((current.get(username) or {}).get("work_delta", "0")))
        prev_share = int((previous.get(username) or {}).get("share_delta", 0))
        prev_work = Decimal(str((previous.get(username) or {}).get("work_delta", "0")))

        rows.append(
            {
                "username": username,
                "current_share_delta": curr_share,
                "current_work_delta": _to_decimal_str(curr_work),
                "previous_share_delta": prev_share,
                "previous_work_delta": _to_decimal_str(prev_work),
                "share_delta_change": curr_share - prev_share,
                "work_delta_change": _to_decimal_str(curr_work - prev_work),
            }
        )
    return rows


def _build_interval_ratio_rows(user_contributions: list[dict[str, object]]) -> list[dict[str, object]]:
    total_share_delta = sum(_to_int(row.get("share_delta")) for row in user_contributions)
    total_work_delta = sum(Decimal(str(row.get("work_delta") or "0")) for row in user_contributions)

    rows: list[dict[str, object]] = []
    for row in user_contributions:
        username = str(row.get("username") or "")
        if not username:
            continue
        share_delta = _to_int(row.get("share_delta"))
        work_delta = Decimal(str(row.get("work_delta") or "0"))
        share_ratio = (
            Decimal(share_delta) / Decimal(total_share_delta)
            if total_share_delta > 0
            else Decimal("0")
        )
        work_ratio = (
            work_delta / total_work_delta
            if total_work_delta > 0
            else Decimal("0")
        )
        rows.append(
            {
                "username": username,
                "share_delta": share_delta,
                "work_delta": _to_decimal_str(work_delta),
                "share_ratio": f"{share_ratio:.8f}",
                "work_ratio": f"{work_ratio:.8f}",
                "share_ratio_percent": f"{(share_ratio * Decimal('100')):.4f}",
                "work_ratio_percent": f"{(work_ratio * Decimal('100')):.4f}",
            }
        )

    rows.sort(key=lambda item: str(item.get("username") or ""))
    return rows


def _build_work_delta_explanation(snapshot_alignment: dict[str, object]) -> dict[str, object]:
    miners = snapshot_alignment.get("miners", []) if isinstance(snapshot_alignment, dict) else []
    per_identity: list[dict[str, object]] = []
    per_user_index: dict[str, dict[str, object]] = {}

    for miner in miners:
        if not isinstance(miner, dict):
            continue
        identity = str(miner.get("identity") or "")
        try:
            username = parse_identity(identity).username
        except ValueError:
            username = "unmapped"

        baseline_work = Decimal(str(miner.get("baseline_work") or "0"))
        current_work = Decimal(str(miner.get("current_work") or "0"))
        work_delta = Decimal(str(miner.get("work_delta") or "0"))

        per_identity.append(
            {
                "username": username,
                "identity": identity,
                "channel_id": miner.get("channel_id"),
                "baseline_work": _to_decimal_str(baseline_work),
                "current_work": _to_decimal_str(current_work),
                "work_delta": _to_decimal_str(work_delta),
                "formula": f"{_to_decimal_str(current_work)} - {_to_decimal_str(baseline_work)} = {_to_decimal_str(work_delta)}",
                "reset_detected": bool(miner.get("reset_detected", False)),
            }
        )

        user_row = per_user_index.setdefault(
            username,
            {
                "username": username,
                "identity_count": 0,
                "baseline_work_sum": Decimal("0"),
                "current_work_sum": Decimal("0"),
                "work_delta_sum": Decimal("0"),
            },
        )
        user_row["identity_count"] = int(user_row["identity_count"]) + 1
        user_row["baseline_work_sum"] = Decimal(str(user_row["baseline_work_sum"])) + baseline_work
        user_row["current_work_sum"] = Decimal(str(user_row["current_work_sum"])) + current_work
        user_row["work_delta_sum"] = Decimal(str(user_row["work_delta_sum"])) + work_delta

    per_user = [
        {
            "username": str(row["username"]),
            "identity_count": int(row["identity_count"]),
            "baseline_work_sum": _to_decimal_str(row["baseline_work_sum"]),
            "current_work_sum": _to_decimal_str(row["current_work_sum"]),
            "work_delta_sum": _to_decimal_str(row["work_delta_sum"]),
            "formula": (
                f"{_to_decimal_str(row['current_work_sum'])} - "
                f"{_to_decimal_str(row['baseline_work_sum'])} = {_to_decimal_str(row['work_delta_sum'])}"
            ),
        }
        for _, row in sorted(per_user_index.items(), key=lambda item: item[0])
    ]

    return {
        "source_metric": "accepted_work_total",
        "description": (
            "Work delta is calculated from stored metric snapshots, not read directly from a translator "
            "endpoint field named work_delta. For each identity/channel in the settlement window, the service "
            "subtracts baseline accepted_work_total from current accepted_work_total, then sums those positive deltas by user."
        ),
        "reset_rule": "If accepted_work_total goes backwards, it is treated as a reset and that negative step contributes 0.",
        "per_user": per_user,
        "per_identity": per_identity,
    }


def _postgres_read_diagnostics(
    *,
    read_source: str,
    effective_read_mode: str,
    fallback_used: bool,
    endpoint_id: str,
) -> dict[str, object]:
    return {
        "read_source": read_source,
        "effective_read_mode": effective_read_mode,
        "fallback_used": fallback_used,
        "postgres_read_endpoint": endpoint_id,
    }


def _postgres_read_error_response(settings, endpoint_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "status": "error",
            "error": "Postgres candidate read failed.",
            "read_diagnostics": _postgres_read_diagnostics(
                read_source="postgres",
                effective_read_mode=settings.effective_postgres_read_mode,
                fallback_used=False,
                endpoint_id=endpoint_id,
            ),
        },
    )


def _postgres_read_endpoint_is_allowed(settings, endpoint_id: str) -> bool:
    return (
        settings.postgres_ledger_reads_enabled
        and settings.effective_postgres_read_mode in POSTGRES_CANDIDATE_READ_MODES
        and endpoint_id in settings.postgres_read_allowed_endpoints
    )


def _postgres_candidate_read_has_public_settlement_id_mapping(endpoint_id: str) -> bool:
    return True


def _postgres_shadow_audit_allows_candidate_read(session: Session) -> bool:
    payload, status_code = audit_postgres_shadow_settlements(
        session,
        limit=10,
        include_details=False,
    )
    return (
        status_code == 200
        and payload.get("comparison_status") == "matched"
        and _to_int(payload.get("mismatched_count")) == 0
        and _to_int(payload.get("not_found_count")) == 0
        and _to_int(payload.get("error_count")) == 0
    )


def _should_use_postgres_candidate_read(settings, endpoint_id: str, session: Session) -> bool:
    if settings.postgres_primary_session_enabled:
        return True
    if not _postgres_read_endpoint_is_allowed(settings, endpoint_id):
        return False
    if not _postgres_candidate_read_has_public_settlement_id_mapping(endpoint_id):
        return False
    if not settings.postgres_ledger_read_require_shadow_match:
        return True
    return _postgres_shadow_audit_allows_candidate_read(session)


def _get_postgres_candidate_read_repository() -> PostgresLedgerRepository:
    return get_postgres_shadow_compare_repository()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.on_event("startup")
def on_startup() -> None:
    global _SERVICE_STARTED_AT
    settings = load_settings()
    _SERVICE_STARTED_AT = datetime.now(UTC).replace(tzinfo=None)
    try:
        archived_log = rotate_payout_audit_log(settings.payout_audit_log_path)
    except OSError:
        archived_log = None

    if settings.enable_startup_reconciliation_hook:
        try:
            with _new_session() as session:
                run_startup_reconciliation_hook(session, settings)
        except Exception as exc:
            _write_scheduler_event(
                "startup_reconciliation_hook_failed",
                {
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )

    if not settings.scheduler_enabled:
        _write_scheduler_event(
            "scheduler_disabled",
            {
                "reason": "SCHEDULER_ENABLED is false",
                "raw_env_value": os.getenv("SCHEDULER_ENABLED"),
                "archived_log_path": archived_log,
            },
        )
        return

    scheduler = start_scheduler()
    scheduler.add_job(
        _run_scheduled_cycle,
        "interval",
        seconds=max(1, int(settings.scheduler_interval_seconds)),
        id="settlement-cycle",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    _write_scheduler_event(
        "scheduler_started",
        {
            "interval_seconds": int(max(1, int(settings.scheduler_interval_seconds))),
            "reward_mode": _normalize_reward_mode(settings.reward_mode),
            "channels_url_configured": bool(settings.translator_channels_url),
            "audit_log_path": settings.payout_audit_log_path,
            "archived_log_path": archived_log,
            "payout_interval_minutes": int(settings.payout_interval_minutes),
        },
    )


@app.on_event("shutdown")
def on_shutdown() -> None:
    settings = load_settings()
    stop_scheduler()
    if settings.scheduler_enabled:
        _write_scheduler_event("scheduler_stopped", {})


def _write_scheduler_event(event_type: str, payload: dict[str, object]) -> None:
    settings = load_settings()
    event = {
        "event_type": event_type,
        "timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        "payload": payload,
    }
    try:
        write_payout_audit_log(settings.payout_audit_log_path, event)
    except OSError:
        pass


def _run_scheduled_cycle() -> None:
    started_at = datetime.now(UTC).replace(tzinfo=None)
    _write_scheduler_event("scheduler_cycle_started", {"started_at": started_at.isoformat()})
    try:
        result = _execute_settlement_cycle(force_settlement=False)
        _write_scheduler_event(
            "scheduler_cycle_completed",
            {
                "settlement": result.get("settlement", {}),
                "snapshots_created": result.get("snapshots_created", 0),
                "settlement_skipped": bool(result.get("settlement_skipped", False)),
            },
        )
    except Exception as exc:
        _write_scheduler_event(
            "scheduler_cycle_failed",
            {
                "error": str(exc),
                "traceback": traceback.format_exc(),
            },
        )


def _new_session() -> Session:
    settings = load_settings()
    if settings.postgres_primary_session_enabled:
        try:
            engine = make_postgres_engine()
            session_factory = make_postgres_session_factory(engine)
            return session_factory()
        except Exception:
            if not settings.postgres_primary_session_fallback_to_sqlite:
                raise

    if settings.sqlite_retirement_mode_enabled:
        raise RuntimeError(
            "SQLite retirement mode is enabled but Postgres primary session is unavailable. "
            "Set POSTGRES_PRIMARY_SESSION_ENABLED=true and POSTGRES_PRIMARY_SESSION_FALLBACK_TO_SQLITE=false."
        )

    init_db(settings.db_path)
    engine = make_engine(settings.db_path)
    session_factory = make_session_factory(engine)
    return session_factory()


def _to_decimal_str(value: object) -> str:
    return f"{Decimal(str(value or 0)):.8f}"


def _as_utc_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _btc_to_sats(value: object) -> int:
    sats = Decimal(str(value or 0)) * _SATS_PER_BTC
    integral_sats = sats.to_integral_value()
    if sats != integral_sats:
        raise ValueError(f"Expected 8-decimal BTC value, got {value!r}")
    return int(integral_sats)


def _load_settlement_payout_rows(
    session: Session,
    settlement_id: int,
) -> list[tuple[UserPayout, User]]:
    settings = load_settings()
    if getattr(settings, "postgres_primary_session_enabled", False):
        try:
            postgres_repo = PostgresLedgerRepository(
                make_postgres_session_factory(make_postgres_engine())
            )
            # Get user credits with user data
            credit_rows = postgres_repo.list_settlement_user_credits_with_users(settlement_id)
            # Get work data for payout_fraction
            work_rows = postgres_repo.list_settlement_user_work_with_users(settlement_id)
            work_by_user: dict[str, dict[str, object]] = {
                row["username"]: row for row in work_rows
            }
            
            # Create payout-like and user-like objects
            class PayoutLike:
                def __init__(self, **attrs):
                    for k, v in attrs.items():
                        setattr(self, k, v)
            
            class UserLike:
                def __init__(self, username: str):
                    self.username = username
            
            result = []
            for credit_row in credit_rows:
                username = credit_row["username"]
                work_row = work_by_user.get(username, {})
                
                payout = PayoutLike(
                    payout_fraction=Decimal(str(work_row.get("payout_fraction", 0))),
                    contribution_value=Decimal(str(work_row.get("share_delta", 0))),
                    amount_btc=Decimal(str(int(credit_row["amount_sats"] or 0))) / Decimal("100000000"),
                    idempotency_key=credit_row["idempotency_key"],
                    status=credit_row["status"],
                )
                user = UserLike(username)
                result.append((payout, user))
            
            return result
        except Exception:
            pass  # Fall back to SQLite below
    
    # SQLite fallback
    return session.execute(
        select(UserPayout, User)
        .join(User, User.id == UserPayout.user_id)
        .where(UserPayout.settlement_id == settlement_id)
        .order_by(User.username.asc(), UserPayout.id.asc())
    ).all()


def _load_settlement_block_models(
    session: Session,
    settlement_id: int,
) -> list[SnapshotBlock]:
    settings = load_settings()
    if getattr(settings, "postgres_primary_session_enabled", False):
        try:
            postgres_repo = PostgresLedgerRepository(
                make_postgres_session_factory(make_postgres_engine())
            )
            rows = postgres_repo.list_settlement_blocks(settlement_id)
            
            # Create block-like objects that match SnapshotBlock interface
            class BlockLike:
                def __init__(self, **attrs):
                    for k, v in attrs.items():
                        setattr(self, k, v)
            
            result = []
            for row in rows:
                block = BlockLike(
                    blockhash=row["blockhash"],
                    found_at=row["found_at"],
                    channel_id=row["channel_id"],
                    worker_identity=row["worker_identity"],
                    source=row["source"],
                    reward_sats=row["reward_sats"],
                    reward_fetched_at=row.get("reward_fetched_at"),
                    created_at=row.get("created_at", datetime.now(UTC)),
                )
                result.append(block)
            
            return result
        except Exception:
            pass  # Fall back to SQLite below
    
    # SQLite fallback
    return session.execute(
        select(SnapshotBlock)
        .where(SnapshotBlock.settlement_id == settlement_id)
        .order_by(SnapshotBlock.found_at.asc(), SnapshotBlock.id.asc())
    ).scalars().all()


def _uses_work_basis_for_shadow_write(
    user_contributions: dict[str, object],
    payout_rows: list[tuple[UserPayout, User]],
) -> bool:
    if any(Decimal(str(getattr(item, "work_delta", 0))) > 0 for item in user_contributions.values()):
        return True

    for payout, user in payout_rows:
        contribution_value = Decimal(str(payout.contribution_value or 0))
        share_delta = Decimal(
            str(getattr(user_contributions.get(user.username), "share_delta", 0))
        )
        if contribution_value != share_delta:
            return True

    return False


def _load_user_total_payout_sats(session: Session, user_id: int) -> int:
    total_btc = session.execute(
        select(func.coalesce(func.sum(UserPayout.amount_btc), 0)).where(UserPayout.user_id == user_id)
    ).scalar_one()
    return _btc_to_sats(total_btc)


def _compact_settlement_raw_snapshots(
    shadow_repository: PostgresLedgerRepository,
    shadow_settlement_id: int,
    payout_period_start: datetime,
    payout_period_end: datetime,
    work_window_start: datetime,
    work_window_end: datetime,
    dry_run: bool = False,
) -> dict[str, object]:
    """Orchestrate snapshot compaction: summarize, upsert, and prune with 3-window retention.
    
    Runs immediately after settlement shadow write. Idempotent on retries.
    """
    try:
        # 1. Summarize raw snapshots for the settlement window
        aggregates = shadow_repository.summarize_raw_snapshots_for_window(
            contribution_window_start=work_window_start,
            contribution_window_end=work_window_end,
        )

        # 2. Upsert summary header row
        summary_row = shadow_repository.upsert_summary_snapshot(
            settlement_id=shadow_settlement_id,
            payout_period_start=payout_period_start,
            payout_period_end=payout_period_end,
            contribution_window_start=work_window_start,
            contribution_window_end=work_window_end,
            snapshot_count=int(aggregates.get("snapshot_count", 0) or 0),
            accepted_shares_sum=int(aggregates.get("accepted_shares_sum", 0) or 0),
            accepted_work_sum=Decimal(str(aggregates.get("accepted_work_sum", 0) or 0)),
        )

        # 3. Replace summary miner rows
        shadow_repository.replace_summary_snapshot_miners(
            summary_snapshot_id=int(summary_row["id"]),
            miners=list(aggregates.get("miners", [])),
        )

        # 4. Prune raw snapshots, keeping latest 3 windows
        prune_stats = shadow_repository.prune_raw_snapshot_windows(
            keep_latest_windows=3,
        )

        return {
            "compaction_enabled": True,
            "status": "completed",
            "settlement_id": shadow_settlement_id,
            "summary_id": int(summary_row["id"]),
            "miner_count": len(aggregates.get("miners", [])),
            "deleted_snapshot_count": int(prune_stats.get("deleted_snapshot_count", 0) or 0),
            "deleted_delta_count": int(prune_stats.get("deleted_delta_count", 0) or 0),
            "pruned_window_count": int(prune_stats.get("pruned_window_count", 0) or 0),
            "dry_run": bool(dry_run),
        }
    except Exception as exc:
        return {
            "compaction_enabled": True,
            "status": "failed",
            "settlement_id": shadow_settlement_id,
            "error": str(exc),
        }


def _shadow_write_postgres_settlement(
    session: Session,
    *,
    settlement_id: int,
    settlement_status: str,
    settlement_period_start: datetime,
    settlement_period_end: datetime,
    settlement_pool_reward_btc: Decimal,
    settlement_total_work: Decimal,
    settlement_total_shares: int,
    work_window_start: datetime,
    work_window_end: datetime,
    settings: object,
) -> dict[str, object]:
    settlement_run_at = _as_utc_aware(settlement_period_end)
    shadow_repository = PostgresLedgerRepository(
        make_postgres_session_factory(make_postgres_engine())
    )

    payout_rows = _load_settlement_payout_rows(session, settlement_id)
    block_rows = _load_settlement_block_models(session, settlement_id)
    contribution_source = "postgres"
    try:
        user_contributions = compute_user_contribution_deltas_postgres(
            shadow_repository,
            _as_utc_aware(work_window_start),
            _as_utc_aware(work_window_end),
        )
    except Exception:
        contribution_source = "sqlite_fallback"
        user_contributions = compute_user_contribution_deltas(session, work_window_start, work_window_end)
    use_work_basis = _uses_work_basis_for_shadow_write(user_contributions, payout_rows)
    maturity_offset_minutes = max(
        0,
        int((settlement_period_end - work_window_end).total_seconds() // 60),
    )

    shadow_settlement = shadow_repository.upsert_settlement_window(
        sqlite_settlement_id=settlement_id,
        settlement_run_at=settlement_run_at,
        work_window_start=_as_utc_aware(work_window_start),
        work_window_end=_as_utc_aware(work_window_end),
        maturity_offset_minutes=maturity_offset_minutes,
        status=settlement_status,
        total_reward_sats=_btc_to_sats(settlement_pool_reward_btc),
        total_work=settlement_total_work,
        total_shares=settlement_total_shares,
        completed_at=settlement_run_at if settlement_status == "completed" else None,
    )

    shadow_users_by_username: dict[str, dict[str, object]] = {}

    def _shadow_user(username: str) -> dict[str, object]:
        existing = shadow_users_by_username.get(username)
        if existing is not None:
            return existing
        row = shadow_repository.upsert_user(username)
        shadow_users_by_username[username] = row
        return row

    invalid_identity_count = 0
    linked_block_count = 0
    for block_row in block_rows:
        worker_identity = block_row.worker_identity
        if worker_identity:
            try:
                identity_parts = parse_identity(worker_identity)
            except ValueError:
                invalid_identity_count += 1
            else:
                user_row = _shadow_user(identity_parts.username)
                shadow_repository.upsert_miner_identity(
                    user_id=int(user_row["id"]),
                    identity=worker_identity,
                    worker_name=identity_parts.worker,
                    created_at=_as_utc_aware(block_row.created_at),
                )

        shadow_repository.upsert_block_found(
            blockhash=block_row.blockhash,
            found_at=_as_utc_aware(block_row.found_at),
            channel_id=block_row.channel_id,
            worker_identity=worker_identity,
            source=block_row.source,
            created_at=_as_utc_aware(block_row.created_at),
        )

        reward_sats = block_row.reward_sats
        if reward_sats is None or int(reward_sats) <= 0:
            continue

        shadow_repository.upsert_block_reward(
            blockhash=block_row.blockhash,
            reward_sats=int(reward_sats),
            fetched_at=_as_utc_aware(block_row.reward_fetched_at) or settlement_run_at,
        )
        shadow_repository.link_settlement_block(
            settlement_id=int(shadow_settlement["id"]),
            blockhash=block_row.blockhash,
            reward_sats=int(reward_sats),
        )
        linked_block_count += 1

    payout_rows_by_username = {user.username: payout for payout, user in payout_rows}
    usernames = sorted(set(user_contributions.keys()) | set(payout_rows_by_username.keys()))

    for username in usernames:
        shadow_user = _shadow_user(username)
        contribution = user_contributions.get(username)
        share_delta = int(getattr(contribution, "share_delta", 0) or 0)
        work_delta = Decimal(str(getattr(contribution, "work_delta", 0) or 0))
        payout_fraction = Decimal("0")
        payout = payout_rows_by_username.get(username)

        if payout is not None:
            payout_fraction = Decimal(str(payout.payout_fraction or 0))
            if use_work_basis:
                work_delta = Decimal(str(payout.contribution_value or 0))

        shadow_repository.upsert_settlement_user_work(
            settlement_id=int(shadow_settlement["id"]),
            user_id=int(shadow_user["id"]),
            share_delta=share_delta,
            work_delta=work_delta,
            payout_fraction=payout_fraction,
        )

        if payout is None:
            continue

        amount_sats = _btc_to_sats(payout.amount_btc)
        credit = shadow_repository.upsert_settlement_user_credit(
            settlement_id=int(shadow_settlement["id"]),
            user_id=int(shadow_user["id"]),
            amount_sats=amount_sats,
            idempotency_key=payout.idempotency_key,
            status=payout.status,
        )

        if amount_sats > 0 and (
            shadow_repository.get_account_ledger_entry_by_settlement_credit_id(int(credit["id"])) is None
        ):
            shadow_repository.create_account_ledger_entry(
                user_id=int(shadow_user["id"]),
                entry_type="settlement_credit",
                amount_sats=amount_sats,
                direction="credit",
                settlement_credit_id=int(credit["id"]),
                memo=f"sqlite-shadow settlement {settlement_id}",
                created_at=settlement_run_at,
            )

        shadow_repository.set_account_balance(
            user_id=int(shadow_user["id"]),
            balance_sats=_load_user_total_payout_sats(session, payout.user_id),
            updated_at=settlement_run_at,
        )

    # Trigger snapshot compaction immediately after successful shadow write
    compaction_stats = _compact_settlement_raw_snapshots(
        shadow_repository=shadow_repository,
        shadow_settlement_id=int(shadow_settlement["id"]),
        payout_period_start=_as_utc_aware(settlement_period_start),
        payout_period_end=_as_utc_aware(settlement_period_end),
        work_window_start=_as_utc_aware(work_window_start),
        work_window_end=_as_utc_aware(work_window_end),
        dry_run=settings.dry_run,
    )

    return {
        "enabled": True,
        "status": "completed",
        "settlement_id": settlement_id,
        "shadow_settlement_id": int(shadow_settlement["id"]),
        "contribution_source": contribution_source,
        "user_count": len(usernames),
        "payout_credit_count": len(payout_rows),
        "linked_block_count": linked_block_count,
        "invalid_identity_count": invalid_identity_count,
        "compaction": compaction_stats,
    }


def _normalize_reward_mode(value: str) -> str:
    mode = (value or "").strip().lower()
    return mode if mode in {"manual", "blocks"} else "blocks"


def _compute_interval_blocks_delta(
    session: Session,
    current_blocks_found_by_channel: dict[int, int],
    *,
    settings=None,
    postgres_repository: PostgresLedgerRepository | None = None,
) -> tuple[int, list[dict[str, int | bool]]]:
    use_postgres = bool(
        settings is not None
        and getattr(settings, "postgres_primary_session_enabled", False)
        and postgres_repository is not None
    )

    if use_postgres:
        rows = postgres_repository.list_block_counter_state()
        previous_by_channel = {int(row.get("channel_id") or 0): int(row.get("last_blocks_found_total") or 0) for row in rows}
    else:
        rows = session.execute(select(BlockCounterState)).scalars().all()
        previous_by_channel = {int(row.channel_id): int(row.last_blocks_found_total or 0) for row in rows}
    state_by_channel = {int(row["channel_id"] if use_postgres else row.channel_id): row for row in rows}

    details: list[dict[str, int | bool]] = []
    interval_blocks = 0
    now_aware = datetime.now(UTC)
    now_naive = now_aware.replace(tzinfo=None)

    for channel_id, current in sorted(current_blocks_found_by_channel.items(), key=lambda item: item[0]):
        previous = int(previous_by_channel.get(channel_id, 0))
        reset_detected = current < previous
        delta = current - previous if not reset_detected else 0
        interval_blocks += delta

        state = state_by_channel.get(channel_id)
        if state is None:
            if use_postgres:
                state = postgres_repository.upsert_block_counter_state(
                    channel_id=channel_id,
                    last_blocks_found_total=current,
                    updated_at=now_aware,
                )
                state_by_channel[channel_id] = state
            else:
                state = BlockCounterState(
                    channel_id=channel_id,
                    last_blocks_found_total=current,
                    updated_at=now_naive,
                )
                session.add(state)
                state_by_channel[channel_id] = state
        else:
            if use_postgres:
                postgres_repository.upsert_block_counter_state(
                    channel_id=channel_id,
                    last_blocks_found_total=current,
                    updated_at=now_aware,
                )
            else:
                state.last_blocks_found_total = current
                state.updated_at = now_naive

        details.append(
            {
                "channel_id": channel_id,
                "previous_blocks_found": previous,
                "current_blocks_found": current,
                "delta_blocks": delta,
                "reset_detected": reset_detected,
            }
        )

    session.flush()
    return interval_blocks, details


@app.get("/service-metrics")
def service_metrics() -> dict:
    with _new_session() as session:
        settings = load_settings()
        if settings.postgres_primary_session_enabled:
            summary = _get_postgres_candidate_read_repository().get_service_metrics_summary()
            return build_service_metrics_payload(summary)
        else:
            settlements_total = session.execute(select(func.count(Settlement.id))).scalar_one()
            payouts_sent_total = session.execute(
                select(func.count(UserPayout.id)).where(UserPayout.status == "sent")
            ).scalar_one()
            payout_failures_total = session.execute(
                select(func.count(PayoutEvent.id)).where(PayoutEvent.status == "pending_sent")
            ).scalar_one()
            last_settlement_timestamp = session.execute(select(func.max(Settlement.period_end))).scalar_one()

    return build_service_metrics_payload(
        {
            "settlements_total": settlements_total,
            "payouts_sent_total": payouts_sent_total,
            "payout_failures_total": payout_failures_total,
            "last_settlement_timestamp": last_settlement_timestamp,
        }
    )


@app.get("/audit/logs")
def audit_logs(limit: int = 50) -> dict:
    settings = load_settings()
    payload = read_recent_audit_entries(settings.payout_audit_log_path, limit=limit)
    payload["scheduler_enabled"] = bool(settings.scheduler_enabled)
    payload["scheduler_interval_seconds"] = int(settings.scheduler_interval_seconds)
    return payload


@app.get("/audit/settlements")
def audit_settlements(limit: int = 120):
    settings = load_settings()
    endpoint_id = POSTGRES_READ_ENDPOINT_SETTLEMENT_HISTORY
    with _new_session() as session:
        use_postgres = _should_use_postgres_candidate_read(settings, endpoint_id, session)
        if use_postgres:
            try:
                payload = _read_postgres_settlement_history(limit=limit)
                payload["read_diagnostics"] = _postgres_read_diagnostics(
                    read_source="postgres",
                    effective_read_mode=settings.effective_postgres_read_mode,
                    fallback_used=False,
                    endpoint_id=endpoint_id,
                )
                return payload
            except Exception:
                if not settings.postgres_ledger_read_fallback_to_sqlite:
                    return _postgres_read_error_response(settings, endpoint_id)
                if settings.postgres_primary_session_enabled:
                    return _postgres_read_error_response(settings, endpoint_id)

                payload = _read_sqlite_settlement_history(settings, session, limit=limit)
                payload["read_diagnostics"] = _postgres_read_diagnostics(
                    read_source="sqlite_fallback",
                    effective_read_mode=settings.effective_postgres_read_mode,
                    fallback_used=True,
                    endpoint_id=endpoint_id,
                )
                return payload

        if should_fail_closed_on_postgres_primary(
            postgres_primary_session_enabled=settings.postgres_primary_session_enabled,
            sqlite_retirement_mode_enabled=settings.sqlite_retirement_mode_enabled,
        ):
            return _postgres_read_error_response(settings, endpoint_id)
        return _read_sqlite_settlement_history(settings, session, limit=limit)


def _read_sqlite_settlement_history(settings, session: Session, *, limit: int) -> dict:
    payload = read_recent_audit_entries(settings.payout_audit_log_path, limit=max(limit * 3, 500))

    attempts: list[dict[str, object]] = []
    scheduler_events: list[dict[str, object]] = []
    for entry in payload.get("entries", []):
        if "attempt_id" in entry:
            attempts.append(entry)
        elif entry.get("event_type"):
            scheduler_events.append(entry)

    attempts.sort(key=lambda row: str(row.get("attempted_at") or ""))

    settlement_ids = [
        _to_int((entry.get("settlement") or {}).get("settlement_id"))
        for entry in attempts
        if _to_int((entry.get("settlement") or {}).get("settlement_id")) > 0
    ]
    blocks_by_settlement = _load_block_rows_by_settlement(session, settlement_ids)

    normalized: list[dict[str, object]] = []
    previous_payout_settlement_id: int | None = None
    previous_payout_contributions: list[dict[str, object]] = []

    for attempt in attempts:
        settlement = attempt.get("settlement", {})
        checks = attempt.get("checks", {})
        payout_rows = attempt.get("payout_rows", [])
        block_reward = attempt.get("block_reward", {})
        user_contributions = attempt.get("user_contributions", [])
        snapshot_alignment = attempt.get("snapshot_alignment", {})
        snapshot_total_shares = _to_int(snapshot_alignment.get("total_share_delta"))
        snapshot_total_work = _to_decimal_str(snapshot_alignment.get("total_work_delta") or "0")
        interval_ratio_rows = _build_interval_ratio_rows(user_contributions)
        contribution_window_start = attempt.get("contribution_window_start") or attempt.get("period_start")
        contribution_window_end = attempt.get("contribution_window_end") or attempt.get("period_end")

        payout_count = len(payout_rows)
        settlement_id = settlement.get("settlement_id")
        payout_user_breakdown = _payout_user_breakdown(payout_rows, user_contributions)
        block_rows = blocks_by_settlement.get(_to_int(settlement_id), [])

        previous_payout_comparison: list[dict[str, object]] = []
        if payout_count > 0:
            previous_payout_comparison = _compare_with_previous_payout(
                user_contributions,
                previous_payout_contributions,
            )

        interval_blocks = int(
            block_reward.get("interval_blocks")
            or block_reward.get("matured_hash_count")
            or 0
        )

        normalized.append(
            {
                "attempt_id": attempt.get("attempt_id"),
                "attempted_at": attempt.get("attempted_at"),
                "period_start": attempt.get("period_start"),
                "period_end": attempt.get("period_end"),
                "contribution_window_start": contribution_window_start,
                "contribution_window_end": contribution_window_end,
                "settlement_id": settlement_id,
                "status": settlement.get("status"),
                "reward_mode": settlement.get("reward_mode"),
                "pool_reward_btc": settlement.get("pool_reward_btc"),
                "carry_btc": settlement.get("carry_btc"),
                "total_shares": settlement.get("total_shares", 0),
                "total_work": settlement.get("total_work"),
                "snapshot_total_shares": snapshot_total_shares,
                "snapshot_total_work": snapshot_total_work,
                "user_count": len(payout_rows),
                "payout_count": payout_count,
                "payout_total_btc": _to_decimal_str(_sum_payout_amount(payout_rows)),
                "unrewarded_user_count": checks.get("unrewarded_user_count", 0),
                "interval_blocks": interval_blocks,
                "computed_reward_btc": block_reward.get("computed_reward_btc", "0.00000000"),
                "settlement_reward_btc": block_reward.get("settlement_reward_btc", settlement.get("pool_reward_btc")),
                "block_rows": block_rows,
                "payout_user_breakdown": payout_user_breakdown,
                "interval_ratio_rows": interval_ratio_rows,
                "work_delta_explanation": _build_work_delta_explanation(snapshot_alignment),
                "last_payout_settlement_id": previous_payout_settlement_id,
                "last_payout_contributions": previous_payout_contributions,
                "payout_vs_last_payout": previous_payout_comparison,
                "raw": attempt,
            }
        )

        if payout_count > 0:
            previous_payout_settlement_id = _to_int(settlement_id)
            previous_payout_contributions = list(user_contributions)

    normalized = normalized[-limit:]
    normalized.sort(key=lambda row: str(row.get("attempted_at") or ""), reverse=True)
    scheduler_events = scheduler_events[-20:]

    return {
        "log_path": payload.get("log_path"),
        "exists": payload.get("exists", False),
        "entry_count": payload.get("entry_count", 0),
        "invalid_line_count": payload.get("invalid_line_count", 0),
        "scheduler_enabled": bool(settings.scheduler_enabled),
        "scheduler_interval_seconds": int(settings.scheduler_interval_seconds),
        "scheduler_events": scheduler_events,
        "settlements": normalized,
    }


def _postgres_settlement_history_rows(limit: int) -> list[dict[str, object]]:
    repository = _get_postgres_candidate_read_repository()
    return repository.list_settlement_history(limit=max(int(limit), 0))


def _postgres_history_user_contributions(
    user_work_rows: list[dict[str, object]],
    baseline_work_by_username: dict[str, Decimal] | None = None,
) -> list[dict[str, object]]:
    """Build user contribution rows including computed cumulative current_work for baseline chaining.
    
    Each row includes:
    - work_delta: from settlement_user_work table
    - current_work: computed as baseline + work_delta (for use as next cycle's baseline)
    - baseline_work: passed from prior cycle (for reference/debugging)
    """
    if baseline_work_by_username is None:
        baseline_work_by_username = {}
    
    result = []
    for row in user_work_rows:
        username = str(row.get("username") or "")
        share_delta = _to_int(row.get("share_delta"))
        work_delta = Decimal(str(row.get("work_delta") or "0"))
        baseline_work = baseline_work_by_username.get(username, Decimal("0"))
        current_work = baseline_work + work_delta
        
        result.append(
            {
                "username": username,
                "share_delta": share_delta,
                "work_delta": _to_decimal_str(work_delta),
                "baseline_work": _to_decimal_str(baseline_work),
                "current_work": _to_decimal_str(current_work),
            }
        )
    return result


def _postgres_history_payout_breakdown(
    credit_rows: list[dict[str, object]],
    user_work_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    work_by_username = {str(row.get("username") or ""): row for row in user_work_rows}
    breakdown: list[dict[str, object]] = []
    for credit in credit_rows:
        username = str(credit.get("username") or "")
        work_row = work_by_username.get(username, {})
        contribution_value = (
            work_row.get("work_delta")
            if Decimal(str(work_row.get("work_delta") or "0")) > 0
            else work_row.get("share_delta", 0)
        )
        breakdown.append(
            {
                "username": username,
                "amount_btc": _sats_to_btc_str(credit.get("amount_sats")),
                "status": credit.get("status"),
                "payout_fraction": str(work_row.get("payout_fraction") or "0"),
                "contribution_value": _to_decimal_str(contribution_value or "0"),
                "share_delta": _to_int(work_row.get("share_delta")),
                "work_delta": _to_decimal_str(work_row.get("work_delta") or "0"),
            }
        )
    return breakdown


def _postgres_history_block_rows(block_rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "found_at": row["found_at"].isoformat() if row.get("found_at") else None,
            "channel_id": _to_int(row.get("channel_id")),
            "worker_identity": row.get("worker_identity"),
            "blockhash": row.get("blockhash"),
            "source": row.get("source"),
            "reward_sats": _to_int(row.get("reward_sats")),
            "reward_btc": _sats_to_btc_str(row.get("reward_sats")),
        }
        for row in block_rows
    ]


def _normalize_postgres_settlement_history_rows(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    previous_payout_settlement_id: int | None = None
    previous_payout_contributions: list[dict[str, object]] = []
    previous_cycle_contributions: list[dict[str, object]] = []

    for row in sorted(rows, key=lambda item: str(item.get("settlement_run_at") or ""), reverse=False):
        credit_rows = list(row.get("user_credits") or [])
        user_work_rows = list(row.get("user_work") or [])
        block_rows = list(row.get("settlement_blocks") or [])
        
        # Extract baseline_work from previous cycle's computed current_work
        baseline_work_by_username: dict[str, Decimal] = {
            str(contrib.get("username") or ""): Decimal(str(contrib.get("current_work") or "0"))
            for contrib in previous_cycle_contributions
        }
        
        summary_snapshot = row.get("summary_snapshot") if isinstance(row.get("summary_snapshot"), dict) else {}
        summary_snapshot_miners = list(row.get("summary_snapshot_miners") or [])
        snapshot_alignment_miners: list[dict[str, object]] = []
        snapshot_latest_state: list[dict[str, object]] = []
        for miner in summary_snapshot_miners:
            if not isinstance(miner, dict):
                continue
            identity = str(miner.get("worker_identity") or "")
            channel_id = _to_int(miner.get("channel_id"))
            snapshot_count = _to_int(miner.get("snapshot_count"))
            shares_sum = _to_int(miner.get("accepted_shares_sum"))
            work_sum = _to_decimal_str(miner.get("accepted_work_sum") or "0")
            snapshot_alignment_miners.append(
                {
                    "identity": identity,
                    "channel_id": channel_id,
                    "snapshot_count": snapshot_count,
                    "accepted_shares_sum": shares_sum,
                    "accepted_work_sum": work_sum,
                    "baseline_snapshot_id": None,
                    "baseline_at": None,
                    "current_snapshot_id": None,
                    "current_at": None,
                    "baseline_shares": None,
                    "current_shares": None,
                    "share_delta": shares_sum,
                    "baseline_work": "0.00000000",
                    "current_work": work_sum,
                    "work_delta": work_sum,
                    "reset_detected": False,
                }
            )
            snapshot_latest_state.append(
                {
                    "identity": identity,
                    "channel_id": channel_id,
                    "latest_snapshot_id": None,
                    "latest_at": None,
                    "latest_shares": shares_sum,
                    "latest_work": work_sum,
                    "snapshot_count": snapshot_count,
                }
            )
        user_contributions = _postgres_history_user_contributions(user_work_rows, baseline_work_by_username)
        payout_user_breakdown = _postgres_history_payout_breakdown(credit_rows, user_work_rows)
        payout_rows_raw = [
            {
                "username": str(credit.get("username") or ""),
                "amount_btc": _sats_to_btc_str(credit.get("amount_sats")),
                "status": credit.get("status"),
                "idempotency_key": credit.get("idempotency_key"),
            }
            for credit in credit_rows
        ]
        credited_usernames = {
            str(credit.get("username") or "")
            for credit in credit_rows
            if str(credit.get("username") or "")
        }
        unrewarded_users = [
            {
                "username": str(work.get("username") or ""),
                "share_delta": _to_int(work.get("share_delta")),
                "work_delta": _to_decimal_str(work.get("work_delta") or "0"),
                "reason": "no_credit_row_for_settlement",
            }
            for work in user_work_rows
            if str(work.get("username") or "")
            and str(work.get("username") or "") not in credited_usernames
            and (
                _to_int(work.get("share_delta")) > 0
                or Decimal(str(work.get("work_delta") or "0")) > 0
            )
        ]
        payout_count = len(credit_rows)
        settlement_id = _to_int(row.get("id"))

        previous_payout_comparison: list[dict[str, object]] = []
        if payout_count > 0:
            previous_payout_comparison = _compare_with_previous_payout(
                user_contributions,
                previous_payout_contributions,
            )

        attempted_at = row["settlement_run_at"].isoformat() if row.get("settlement_run_at") else None
        work_window_start = row["work_window_start"].isoformat() if row.get("work_window_start") else None
        work_window_end = row["work_window_end"].isoformat() if row.get("work_window_end") else None
        work_window_start_dt = row.get("work_window_start")
        work_window_end_dt = row.get("work_window_end")
        maturity_offset_minutes = max(0, _to_int(row.get("maturity_offset_minutes")))
        if (
            maturity_offset_minutes > 0
            and isinstance(work_window_start_dt, datetime)
            and isinstance(work_window_end_dt, datetime)
        ):
            contribution_window_start = (work_window_start_dt - timedelta(minutes=maturity_offset_minutes)).isoformat()
            contribution_window_end = (work_window_end_dt - timedelta(minutes=maturity_offset_minutes)).isoformat()
        else:
            contribution_window_start = work_window_start
            contribution_window_end = work_window_end
        payout_total_sats = sum(_to_int(credit.get("amount_sats")) for credit in credit_rows)

        normalized.append(
            {
                "attempt_id": None,
                "attempted_at": attempted_at,
                "period_start": work_window_start,
                "period_end": work_window_end,
                "contribution_window_start": contribution_window_start,
                "contribution_window_end": contribution_window_end,
                "settlement_id": settlement_id,
                "status": row.get("status"),
                "reward_mode": None,
                "pool_reward_btc": _sats_to_btc_str(row.get("total_reward_sats")),
                "carry_btc": None,
                "total_shares": _to_int(row.get("total_shares")),
                "total_work": _to_decimal_str(row.get("total_work") or "0"),
                "snapshot_total_shares": _to_int(row.get("total_shares")),
                "snapshot_total_work": _to_decimal_str(row.get("total_work") or "0"),
                "user_count": len(credit_rows),
                "payout_count": payout_count,
                "payout_total_btc": _sats_to_btc_str(payout_total_sats),
                "unrewarded_user_count": len(unrewarded_users),
                "interval_blocks": len(block_rows),
                "computed_reward_btc": _sats_to_btc_str(row.get("total_reward_sats")),
                "settlement_reward_btc": _sats_to_btc_str(row.get("total_reward_sats")),
                "block_rows": _postgres_history_block_rows(block_rows),
                "payout_user_breakdown": payout_user_breakdown,
                "interval_ratio_rows": _build_interval_ratio_rows(user_contributions),
                "work_delta_explanation": _build_postgres_work_delta_explanation(
                    user_contributions,
                    previous_cycle_contributions,
                ),
                "last_payout_settlement_id": previous_payout_settlement_id,
                "last_payout_contributions": previous_payout_contributions,
                "payout_vs_last_payout": previous_payout_comparison,
                "raw": {
                    "read_source": "postgres",
                    "settlement_window_id": _to_int(row.get("id")),
                    "payout_rows": payout_rows_raw,
                    "checks": {
                        "unrewarded_user_count": len(unrewarded_users),
                        "unrewarded_users": unrewarded_users,
                    },
                    "snapshot_alignment": {
                        "total_share_delta": _to_int(
                            summary_snapshot.get("accepted_shares_sum", row.get("total_shares"))
                            if isinstance(summary_snapshot, dict)
                            else row.get("total_shares")
                        ),
                        "total_work_delta": _to_decimal_str(
                            summary_snapshot.get("accepted_work_sum", row.get("total_work") or "0")
                            if isinstance(summary_snapshot, dict)
                            else row.get("total_work") or "0"
                        ),
                        "miners": snapshot_alignment_miners,
                        "latest_snapshot_state": snapshot_latest_state,
                        "coverage": {
                            "tracked_miners_total": len(snapshot_alignment_miners),
                            "snapshots_in_window": _to_int(
                                summary_snapshot.get("snapshot_count", 0)
                                if isinstance(summary_snapshot, dict)
                                else 0
                            ),
                        },
                    },
                },
            }
        )

        if payout_count > 0:
            previous_payout_settlement_id = settlement_id
            previous_payout_contributions = list(user_contributions)
        previous_cycle_contributions = list(user_contributions)

    normalized.sort(key=lambda item: str(item.get("attempted_at") or ""), reverse=True)
    return normalized


def _build_postgres_work_delta_explanation(
    user_work_rows: list[dict[str, object]],
    previous_cycle_contributions: list[dict[str, object]],
) -> dict[str, object]:
    """Build work-delta explanation from settlement_user_work rows for the Postgres authoritative path.

    user_work_rows are already enriched with baseline_work and current_work by _postgres_history_user_contributions,
    so we just surface those computed values.
    """
    per_user: list[dict[str, object]] = []
    for work_row in sorted(user_work_rows, key=lambda r: str(r.get("username") or "")):
        username = str(work_row.get("username") or "")
        baseline_work = Decimal(str(work_row.get("baseline_work") or "0"))
        current_work = Decimal(str(work_row.get("current_work") or "0"))
        work_delta = Decimal(str(work_row.get("work_delta") or "0"))
        per_user.append(
            {
                "username": username,
                "identity_count": 1,
                "baseline_work_sum": _to_decimal_str(baseline_work),
                "current_work_sum": _to_decimal_str(current_work),
                "work_delta_sum": _to_decimal_str(work_delta),
                "formula": (
                    f"{_to_decimal_str(current_work)} - "
                    f"{_to_decimal_str(baseline_work)} = {_to_decimal_str(work_delta)}"
                ),
            }
        )
    return {
        "source_metric": "accepted_work_total",
        "description": (
            "Work delta read from settlement_user_work table (Postgres authoritative path). "
            "Baseline is previous cycle's computed current_work. "
            "Per-snapshot baseline/current counters are only available in the audit log."
        ),
        "reset_rule": "negative steps are treated as counter resets and contribute 0",
        "per_identity": [],
        "per_user": per_user,
    }


def _read_postgres_settlement_history(*, limit: int) -> dict:
    rows = _postgres_settlement_history_rows(limit)
    normalized = _normalize_postgres_settlement_history_rows(rows)
    settings = load_settings()
    audit_payload = read_recent_audit_entries(settings.payout_audit_log_path, limit=500)
    scheduler_events: list[dict[str, object]] = [
        entry
        for entry in audit_payload.get("entries", [])
        if entry.get("event_type") and "attempt_id" not in entry
    ]
    scheduler_events = scheduler_events[-20:]
    return {
        "log_path": settings.payout_audit_log_path,
        "exists": True,
        "entry_count": len(normalized),
        "invalid_line_count": 0,
        "scheduler_enabled": bool(settings.scheduler_enabled),
        "scheduler_interval_seconds": int(settings.scheduler_interval_seconds),
        "scheduler_events": scheduler_events,
        "settlements": normalized,
    }


@app.get("/audit/dashboard", response_class=HTMLResponse)
def audit_dashboard() -> HTMLResponse:
    html = """<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Payout Settlements Dashboard</title>
    <style>
        :root {
            --bg0: #f4f1ea;
            --bg1: #e8dcc8;
            --panel: #fffdfa;
            --ink: #1f1a15;
            --muted: #6a5c4d;
            --good: #1f7a4c;
            --warn: #8a5b00;
            --border: #d7c8b3;
            --accent: #0f5c78;
            --accent-soft: #d7e8ee;
            --shadow: 0 10px 30px rgba(34, 26, 15, 0.12);
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            color: var(--ink);
            font-family: Georgia, "Times New Roman", serif;
            background:
                radial-gradient(1300px 700px at -10% -20%, #f8edd6 10%, transparent 60%),
                radial-gradient(900px 500px at 120% -10%, #d2e8ef 10%, transparent 55%),
                linear-gradient(180deg, var(--bg0), var(--bg1));
            min-height: 100vh;
        }
        .wrap {
            max-width: 1260px;
            margin: 0 auto;
            padding: 24px;
        }
        .top {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 16px;
        }
        h1 {
            margin: 0;
            font-size: clamp(1.4rem, 2.7vw, 2rem);
            letter-spacing: 0.4px;
        }
        .muted { color: var(--muted); font-size: 0.95rem; }
        .btn {
            background: var(--accent);
            color: #fff;
            border: 0;
            border-radius: 10px;
            padding: 10px 14px;
            cursor: pointer;
            box-shadow: var(--shadow);
        }
        .kpis {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 10px;
            margin-bottom: 14px;
        }
        .kpi {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 10px 12px;
            box-shadow: var(--shadow);
            animation: rise 220ms ease;
        }
        .kpi b { display: block; font-size: 1.15rem; margin-top: 4px; }
        .layout {
            display: grid;
            grid-template-columns: 390px 1fr;
            gap: 12px;
            margin-bottom: 12px;
        }
        .panel {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 14px;
            box-shadow: var(--shadow);
            overflow: hidden;
            min-height: 65vh;
        }
        .panel h2 {
            margin: 0;
            font-size: 1rem;
            padding: 11px 12px;
            border-bottom: 1px solid var(--border);
            background: linear-gradient(90deg, #f9f1e5, #eef7fa);
        }
        .list {
            max-height: calc(65vh - 44px);
            overflow: auto;
        }
        .item {
            width: 100%;
            border: 0;
            border-left: 5px solid transparent;
            background: transparent;
            text-align: left;
            padding: 10px 12px;
            border-bottom: 1px solid #eee1d0;
            cursor: pointer;
        }
        .item:hover { background: #f9f5ee; }
        .item.active { background: var(--accent-soft); }
        .item.has-blocks { border-left-color: #1f7a4c; }
        .item.has-payouts { border-left-color: #0f5c78; }
        .item.has-blocks.has-payouts {
            border-left-color: #1f7a4c;
            background: linear-gradient(90deg, #eaf8f0, transparent 35%);
        }
        .line { display: flex; justify-content: space-between; gap: 6px; }
        .badge {
            display: inline-block;
            font-family: ui-monospace, Menlo, Consolas, monospace;
            font-size: 0.78rem;
            border-radius: 999px;
            border: 1px solid var(--border);
            padding: 2px 8px;
            background: #faf4ea;
            color: #563e22;
        }
        .good { color: var(--good); }
        .warn { color: var(--warn); }
        .detail {
            padding: 14px;
            overflow: auto;
            max-height: calc(65vh - 44px);
            font-family: ui-monospace, Menlo, Consolas, monospace;
            font-size: 13px;
            line-height: 1.45;
        }
        .detail-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
            margin-bottom: 12px;
        }
        .box {
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 10px;
            background: #fff;
        }
        pre {
            margin: 0;
            white-space: pre-wrap;
            word-break: break-word;
            background: #f9f6ef;
            border: 1px solid #eadfcd;
            border-radius: 10px;
            padding: 10px;
        }
        .small { font-size: 12px; color: var(--muted); }
        .events {
            padding: 10px 12px;
            max-height: 38vh;
            overflow: auto;
            display: grid;
            gap: 8px;
        }
        .event {
            border: 1px solid var(--border);
            border-radius: 10px;
            background: #fff;
            padding: 10px;
        }
        .event-top {
            display: flex;
            justify-content: space-between;
            gap: 8px;
            margin-bottom: 6px;
            align-items: center;
        }
        .event-type {
            font-family: ui-monospace, Menlo, Consolas, monospace;
            font-size: 12px;
            border: 1px solid var(--border);
            border-radius: 999px;
            padding: 2px 8px;
            background: #f6efe2;
            color: #4f391f;
        }
        @keyframes rise {
            from { transform: translateY(6px); opacity: 0; }
            to { transform: translateY(0); opacity: 1; }
        }
        @media (max-width: 980px) {
            .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .layout { grid-template-columns: 1fr; }
            .panel { min-height: auto; }
            .list, .detail { max-height: 45vh; }
            .events { max-height: 45vh; }
        }
    </style>
</head>
<body>
    <div class=\"wrap\">
        <div class=\"top\">
            <div>
                <h1>Payout Settlements Dashboard</h1>
                <div class=\"muted\">Live view built from payout audit logs</div>
            </div>
            <label class=\"muted\" style=\"display:flex;align-items:center;gap:8px;\">
                <input type=\"checkbox\" id=\"rewardOnly\" />
                Reward/Payout only
            </label>
            <label class=\"muted\" style=\"display:flex;align-items:center;gap:8px;\">
                <input type=\"checkbox\" id=\"autoRefresh\" />
                Auto Refresh (10s)
            </label>
            <button class=\"btn\" id=\"refreshBtn\">Refresh</button>
        </div>

        <div class=\"kpis\" id=\"kpis\"></div>

        <div class=\"layout\">
            <section class=\"panel\">
                <h2>Recent Settlements</h2>
                <div class=\"list\" id=\"list\"></div>
            </section>
            <section class=\"panel\">
                <h2>Settlement Details</h2>
                <div class=\"detail\" id=\"detail\">Loading...</div>
            </section>
        </div>

        <section class=\"panel\">
            <h2>Scheduler & System Events</h2>
            <div class=\"events\" id=\"events\">Loading...</div>
        </section>
    </div>

    <script>
        const state = { data: null, selected: 0, autoRefreshTimer: null };

        function fmtNum(value) {
            if (value === null || value === undefined) return '-';
            const n = Number(value);
            if (Number.isNaN(n)) return String(value);
            return n.toLocaleString();
        }

        function fmtBtc(value) {
            if (value === null || value === undefined) return '0.00000000';
            return String(value);
        }

        function fmtMst(value) {
            if (!value) return '-';
            const raw = String(value).trim();
            const hasTz = /([zZ]|[+-]\\d{2}:\\d{2})$/.test(raw);
            const utc = new Date(hasTz ? raw : `${raw}Z`);
            if (Number.isNaN(utc.getTime())) return value;
            const mst = new Date(utc.getTime() - 7 * 60 * 60 * 1000);
            return `${mst.toISOString().slice(0, 19).replace('T', ' ')} MST`;
        }

        function fmtPeriod(start, end) {
            return `${fmtMst(start)} -> ${fmtMst(end)}`;
        }

        function renderKpis() {
            const kpis = document.getElementById('kpis');
            const rows = (state.data && state.data.settlements) || [];
            const latest = rows[0] || null;
            const blocksInWindow = rows.reduce((acc, row) => acc + Number(row.interval_blocks || 0), 0);
            const payoutsInWindow = rows.reduce((acc, row) => acc + Number(row.payout_count || 0), 0);
            const html = [
                ['Settlements Loaded', fmtNum(rows.length)],
                ['Scheduler', state.data && state.data.scheduler_enabled ? 'Enabled' : 'Disabled'],
                ['Blocks in Window', fmtNum(blocksInWindow)],
                ['Last Settlement ID', latest ? fmtNum(latest.settlement_id) : '-'],
                ['Payout Rows in Window', fmtNum(payoutsInWindow)],
                ['Last Reward BTC', latest ? fmtBtc(latest.pool_reward_btc) : '0.00000000'],
                ['Last Unrewarded Users', latest ? fmtNum(latest.unrewarded_user_count) : '0'],
                ['Invalid Log Lines', fmtNum(state.data ? state.data.invalid_line_count : 0)],
            ];
            kpis.innerHTML = html.map(([k, v]) => `<div class=\"kpi\"><span class=\"muted\">${k}</span><b>${v}</b></div>`).join('');
        }

        function renderList() {
            const list = document.getElementById('list');
            const rows = (state.data && state.data.settlements) || [];
            const rewardOnly = Boolean(document.getElementById('rewardOnly').checked);
            const visibleRows = rewardOnly
                ? rows.filter((row) => Number(row.interval_blocks || 0) > 0 || Number(row.payout_count || 0) > 0)
                : rows;

            if (state.selected >= visibleRows.length) {
                state.selected = 0;
            }
            list.innerHTML = visibleRows.map((row, idx) => {
                const reward = Number(row.pool_reward_btc || 0);
                const hasBlocks = Number(row.interval_blocks || 0) > 0;
                const hasPayouts = Number(row.payout_count || 0) > 0;
                const parts = ['item'];
                if (idx === state.selected) parts.push('active');
                if (hasBlocks) parts.push('has-blocks');
                if (hasPayouts) parts.push('has-payouts');
                const cls = parts.join(' ');
                return `
                    <button class=\"${cls}\" data-idx=\"${idx}\">
                        <div class=\"line\"><strong>#${row.settlement_id || '-'}</strong><span class=\"badge\">${row.status || 'unknown'}</span></div>
                        <div class=\"line small\"><span>${fmtMst(row.attempted_at)}</span><span>${row.reward_mode || '-'}</span></div>
                        <div class=\"line small\"><span>${fmtPeriod(row.period_start, row.period_end)}</span></div>
                        <div class=\"line\"><span class=\"${reward > 0 ? 'good' : 'warn'}\">reward ${fmtBtc(row.pool_reward_btc)} BTC</span><span>payouts ${fmtNum(row.payout_count)}</span></div>
                        <div class=\"line small\"><span>blocks ${fmtNum(row.interval_blocks)}</span><span>unrewarded ${fmtNum(row.unrewarded_user_count)}</span></div>
                    </button>
                `;
            }).join('');

            [...list.querySelectorAll('.item')].forEach((el) => {
                el.addEventListener('click', () => {
                    state.selected = Number(el.getAttribute('data-idx'));
                    renderList();
                    renderDetail();
                });
            });
        }

        function renderDetail() {
            const panel = document.getElementById('detail');
            const rows = (state.data && state.data.settlements) || [];
            const rewardOnly = Boolean(document.getElementById('rewardOnly').checked);
            const visibleRows = rewardOnly
                ? rows.filter((row) => Number(row.interval_blocks || 0) > 0 || Number(row.payout_count || 0) > 0)
                : rows;
            const row = visibleRows[state.selected];
            if (!row) {
                panel.textContent = 'No settlement data available.';
                return;
            }
            const raw = row.raw || {};
                        const workDeltaExplanation = row.work_delta_explanation || {};
                        const workPerUser = workDeltaExplanation.per_user || [];
                        const workPerIdentity = workDeltaExplanation.per_identity || [];
            const intervalRatioRows = row.interval_ratio_rows || [];
            const payoutRows = raw.payout_rows || [];
            const unrewarded = (raw.checks && raw.checks.unrewarded_users) || [];
            const channels = (raw.block_reward && raw.block_reward.channels) || [];
            const userBreakdown = row.payout_user_breakdown || [];
            const compareWithLast = row.payout_vs_last_payout || [];
            const blockRows = row.block_rows || [];
            const blockReward = raw.block_reward || {};
            const snapshotAlignment = raw.snapshot_alignment || {};
            const snapshotRows = snapshotAlignment.miners || [];
            const latestSnapshotState = snapshotAlignment.latest_snapshot_state || [];
            const coverage = snapshotAlignment.coverage || {};
            const maturedStart = blockReward.matured_window_start ? fmtMst(blockReward.matured_window_start) : null;
            const maturedEnd = blockReward.matured_window_end ? fmtMst(blockReward.matured_window_end) : null;
            const maturedWindowStr = maturedStart && maturedEnd ? `${maturedStart} → ${maturedEnd}` : '—';
            const contributionWindowStr = fmtPeriod(row.contribution_window_start, row.contribution_window_end);
            const snapshotWindowShares = fmtNum(row.snapshot_total_shares || 0);
            const snapshotWindowWork = row.snapshot_total_work || '0.00000000';
            const payoutRatios = userBreakdown.map((entry) => ({
                username: entry.username,
                payout_fraction: entry.payout_fraction || '0',
                contribution_value: entry.contribution_value || '0.00000000',
                share_delta: entry.share_delta || 0,
                work_delta: entry.work_delta || '0.00000000',
                amount_btc: entry.amount_btc || '0.00000000',
                status: entry.status || '-',
            }));
            panel.innerHTML = `
                <div class="detail-grid">
                    <div class="box"><div class="small">Window Shares / Window Work (From Snapshots)</div><div>${snapshotWindowShares} / ${snapshotWindowWork}</div></div>
                    <div class=\"box\"><div class=\"small\">Attempt</div><div>${row.attempt_id || '-'}</div></div>
                    <div class=\"box\"><div class=\"small\">Settlement Period (MST)</div><div>${fmtPeriod(row.period_start, row.period_end)}</div></div>
                    <div class=\"box\"><div class=\"small\">Contribution Window (MST)</div><div>${contributionWindowStr}</div></div>
                    <div class=\"box\"><div class=\"small\">Matured Reward Window (MST)</div><div>${maturedWindowStr}</div></div>
                    <div class=\"box\"><div class=\"small\">Pool Reward BTC</div><div>${fmtBtc(row.pool_reward_btc)}</div></div>
                    <div class=\"box\"><div class=\"small\">Computed Reward BTC</div><div>${fmtBtc(row.computed_reward_btc)}</div></div>
                    <div class=\"box\"><div class=\"small\">Settlement Reward BTC</div><div>${fmtBtc(row.settlement_reward_btc)}</div></div>
                    <div class=\"box\"><div class=\"small\">Payout Total BTC</div><div>${fmtBtc(row.payout_total_btc)}</div></div>
                    <div class=\"box\"><div class=\"small\">Blocks Found This Cycle</div><div>${fmtNum(row.interval_blocks)}</div></div>
                    <div class=\"box\"><div class=\"small\">Linked Matured Blocks</div><div>${fmtNum(blockRows.length)}</div></div>
                    <div class=\"box\"><div class=\"small\">Snapshot Coverage</div><div>${fmtNum(coverage.snapshots_in_window || 0)} in-window / ${fmtNum(coverage.tracked_miners_total || 0)} miners</div></div>
                </div>
                <div class="box" style="margin-bottom:10px;">
                    <div class="small">How Work Delta Is Calculated</div>
                    <pre>${JSON.stringify({
                        contribution_window_start: row.contribution_window_start || null,
                        contribution_window_end: row.contribution_window_end || null,
                        source_metric: workDeltaExplanation.source_metric || 'accepted_work_total',
                        description: workDeltaExplanation.description || 'current accepted_work_total - baseline accepted_work_total, summed by user',
                        reset_rule: workDeltaExplanation.reset_rule || 'negative steps are treated as counter resets and contribute 0',
                        coverage,
                        per_user: workPerUser,
                    }, null, 2)}</pre>
                </div>
                <div class="box" style="margin-bottom:10px;">
                    <div class="small">Snapshot Rows Used For Delta (Baseline -> Current)</div>
                    <pre>${JSON.stringify(snapshotRows, null, 2)}</pre>
                </div>
                <div class="box" style="margin-bottom:10px;">
                    <div class="small">Latest Snapshot State By Identity</div>
                    <pre>${JSON.stringify(latestSnapshotState, null, 2)}</pre>
                </div>
                <div class="box" style="margin-bottom:10px;">
                    <div class="small">accepted_work_total By Identity In Contribution Window</div>
                    <pre>${JSON.stringify(workPerIdentity, null, 2)}</pre>
                </div>
                <div class="box" style="margin-bottom:10px;">
                    <div class="small">Payout Ratio By User</div>
                    <pre>${JSON.stringify(payoutRatios, null, 2)}</pre>
                </div>
                <div class="box" style="margin-bottom:10px;">
                    <div class="small">Interval Ratio From Snapshot Deltas (user_delta / total_delta)</div>
                    <pre>${JSON.stringify(intervalRatioRows, null, 2)}</pre>
                </div>
                <div class="box" style="margin-bottom:10px;">
                    <div class="small">Matured Blocks And Rewards</div>
                    <pre>${JSON.stringify(blockRows, null, 2)}</pre>
                </div>
                <div class=\"box\" style=\"margin-bottom:10px;\">
                    <div class=\"small\">Payout Split By User (this settlement)</div>
                    <pre>${JSON.stringify(userBreakdown, null, 2)}</pre>
                </div>
                <div class=\"box\" style=\"margin-bottom:10px;\">
                    <div class=\"small\">This Payout vs Last Payout Basis (share/work)</div>
                    <pre>${JSON.stringify(compareWithLast, null, 2)}</pre>
                </div>
                <div class=\"box\" style=\"margin-bottom:10px;\">
                    <div class=\"small\">Payout Rows</div>
                    <pre>${JSON.stringify(payoutRows, null, 2)}</pre>
                </div>
                <div class=\"box\" style=\"margin-bottom:10px;\">
                    <div class=\"small\">Unrewarded Users</div>
                    <pre>${JSON.stringify(unrewarded, null, 2)}</pre>
                </div>
                <div class=\"box\">
                    <div class=\"small\">Block Delta By Channel</div>
                    <pre>${JSON.stringify(channels, null, 2)}</pre>
                </div>
            `;
        }

        function renderEvents() {
            const panel = document.getElementById('events');
            const events = (state.data && state.data.scheduler_events) || [];
            if (!events.length) {
                panel.innerHTML = '<div class=\"muted\">No scheduler/system events found yet in the audit log.</div>';
                return;
            }

            const sorted = [...events].sort((a, b) => {
                return String(b.timestamp || '').localeCompare(String(a.timestamp || ''));
            });

            panel.innerHTML = sorted.map((event) => {
                const eventType = event.event_type || 'unknown_event';
                const eventTime = fmtMst(event.timestamp);
                const payload = event.payload || {};
                return `
                    <div class=\"event\">
                        <div class=\"event-top\">
                            <span class=\"event-type\">${eventType}</span>
                            <span class=\"small\">${eventTime}</span>
                        </div>
                        <pre>${JSON.stringify(payload, null, 2)}</pre>
                    </div>
                `;
            }).join('');
        }

        function updateAutoRefresh() {
            const enabled = Boolean(document.getElementById('autoRefresh').checked);
            if (state.autoRefreshTimer) {
                clearInterval(state.autoRefreshTimer);
                state.autoRefreshTimer = null;
            }
            if (enabled) {
                state.autoRefreshTimer = setInterval(() => {
                    loadData();
                }, 10000);
            }
        }

        async function loadData() {
            const detail = document.getElementById('detail');
            const eventsPanel = document.getElementById('events');
            detail.textContent = 'Loading latest settlements...';
            eventsPanel.textContent = 'Loading scheduler/system events...';
            const res = await fetch('/audit/settlements?limit=200');
            if (!res.ok) {
                detail.textContent = 'Failed to load settlements feed.';
                eventsPanel.textContent = 'Failed to load scheduler/system events.';
                return;
            }
            state.data = await res.json();
            const rows = (state.data && state.data.settlements) || [];
            const firstInformative = rows.findIndex((row) => {
                return Number(row.interval_blocks || 0) > 0
                    || Number(row.payout_count || 0) > 0
                    || Number(row.snapshot_total_shares || 0) > 0
                    || Number(row.snapshot_total_work || 0) > 0;
            });
            state.selected = firstInformative >= 0 ? firstInformative : 0;
            renderKpis();
            renderList();
            renderDetail();
            renderEvents();
        }

        document.getElementById('refreshBtn').addEventListener('click', loadData);
        document.getElementById('autoRefresh').addEventListener('change', updateAutoRefresh);
        document.getElementById('rewardOnly').addEventListener('change', () => {
            state.selected = 0;
            renderList();
            renderDetail();
        });
        updateAutoRefresh();
        loadData();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@app.get("/settlements/latest")
def latest_settlement():
    settings = load_settings()
    endpoint_id = POSTGRES_READ_ENDPOINT_SETTLEMENT_DETAIL
    with _new_session() as session:
        use_postgres = _should_use_postgres_candidate_read(settings, endpoint_id, session)
        if use_postgres:
            try:
                payload = _read_postgres_latest_settlement()
                payload["read_diagnostics"] = _postgres_read_diagnostics(
                    read_source="postgres",
                    effective_read_mode=settings.effective_postgres_read_mode,
                    fallback_used=False,
                    endpoint_id=endpoint_id,
                )
                return payload
            except Exception:
                if not settings.postgres_ledger_read_fallback_to_sqlite:
                    return _postgres_read_error_response(settings, endpoint_id)
                if settings.postgres_primary_session_enabled:
                    return _postgres_read_error_response(settings, endpoint_id)

                payload = _read_sqlite_latest_settlement(session)
                payload["read_diagnostics"] = _postgres_read_diagnostics(
                    read_source="sqlite_fallback",
                    effective_read_mode=settings.effective_postgres_read_mode,
                    fallback_used=True,
                    endpoint_id=endpoint_id,
                )
                return payload

        if should_fail_closed_on_postgres_primary(
            postgres_primary_session_enabled=settings.postgres_primary_session_enabled,
            sqlite_retirement_mode_enabled=settings.sqlite_retirement_mode_enabled,
        ):
            return _postgres_read_error_response(settings, endpoint_id)
        return _read_sqlite_latest_settlement(session)


def _read_sqlite_latest_settlement(session: Session) -> dict:
    settlement = session.execute(
        select(Settlement).order_by(Settlement.period_end.desc(), Settlement.id.desc())
    ).scalar_one_or_none()
    if settlement is None:
        return {"settlement": None, "users": []}

    rows = session.execute(
        select(UserPayout, User)
        .join(User, User.id == UserPayout.user_id)
        .where(UserPayout.settlement_id == settlement.id)
        .order_by(User.username.asc(), UserPayout.id.asc())
    ).all()

    return {
        "settlement": {
            "settlement_id": settlement.id,
            "status": settlement.status,
            "period_start": settlement.period_start.isoformat(),
            "period_end": settlement.period_end.isoformat(),
            "pool_reward_btc": _to_decimal_str(settlement.pool_reward_btc),
            "total_shares": int(settlement.total_shares or 0),
            "total_work": _to_decimal_str(settlement.total_work),
        },
        "users": [
            {
                "username": user.username,
                "contribution_value": _to_decimal_str(payout.contribution_value),
                "payout_fraction": str(payout.payout_fraction),
                "amount_btc": _to_decimal_str(payout.amount_btc),
                "status": payout.status,
            }
            for payout, user in rows
        ],
    }


def _read_postgres_latest_settlement() -> dict:
    repository = _get_postgres_candidate_read_repository()
    return build_latest_settlement_payload(repository.get_latest_settlement_detail())


def _format_block_row(row: dict[str, object]) -> dict[str, object]:
    found_time = row.get("found_time")
    return {
        "blockhash": row.get("blockhash"),
        "found_time": found_time.isoformat() if isinstance(found_time, datetime) else None,
        "found_time_unix": _to_int(row.get("found_time_unix")),
        "worker_identity": row.get("worker_identity"),
        "channel_id": _to_int(row.get("channel_id")),
        "source": row.get("source"),
        "proof_type": row.get("proof_type"),
    }


def _compute_avg_block_interval_seconds(rows: list[dict[str, object]]) -> float | None:
    """Compute average seconds between consecutive found_time values.

    ``rows`` must be ordered newest-first (desc). Returns None if fewer than 2 rows.
    """
    times: list[datetime] = []
    for row in rows:
        ft = row.get("found_time")
        if isinstance(ft, datetime):
            times.append(ft)
    if len(times) < 2:
        return None
    # times are desc; compute gaps between consecutive pairs
    gaps: list[float] = []
    for i in range(len(times) - 1):
        delta = times[i] - times[i + 1]
        gaps.append(abs(delta.total_seconds()))
    return sum(gaps) / len(gaps)


def _read_translator_block_found_stats() -> dict[str, object]:
    repository = _get_postgres_candidate_read_repository()
    now = datetime.now(UTC)
    # Fetch enough rows to compute a meaningful avg interval (up to 50), display top 3
    recent_rows = repository.list_translator_candidate_blocks(limit=50, order="desc")

    def _window_count(hours: int) -> int:
        return repository.count_translator_candidate_blocks(start_time=now - timedelta(hours=hours), end_time=now)

    recent_blocks = [_format_block_row(row) for row in recent_rows[:3]]
    avg_interval_seconds = _compute_avg_block_interval_seconds(recent_rows)

    return {
        "status": "ok",
        "read_source": "postgres.translator_candidate_blocks",
        "generated_at": now.isoformat(),
        "recent_blocks": recent_blocks,
        "avg_block_interval_seconds": avg_interval_seconds,
        "avg_block_interval_basis": len(recent_rows),
        "counts": {
            "last_1h": _window_count(1),
            "last_8h": _window_count(8),
            "last_24h": _window_count(24),
        },
    }


@app.get("/audit/block-found")
def audit_block_found() -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=_read_translator_block_found_stats())
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "error": "Unable to read translator candidate blocks.",
                "details": str(exc),
            },
        )


@app.get("/audit/block-found/dashboard", response_class=HTMLResponse)
def audit_block_found_dashboard() -> HTMLResponse:
    html = """<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Block Found Dashboard</title>
    <style>
        :root {
            --bg0: #f4f1ea;
            --bg1: #e8dcc8;
            --panel: #fffdfa;
            --ink: #1f1a15;
            --muted: #6a5c4d;
            --border: #d7c8b3;
            --accent: #0f5c78;
            --shadow: 0 10px 30px rgba(34, 26, 15, 0.12);
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            color: var(--ink);
            font-family: Georgia, \"Times New Roman\", serif;
            background:
                radial-gradient(1300px 700px at -10% -20%, #f8edd6 10%, transparent 60%),
                radial-gradient(900px 500px at 120% -10%, #d2e8ef 10%, transparent 55%),
                linear-gradient(180deg, var(--bg0), var(--bg1));
            min-height: 100vh;
        }
        .wrap {
            max-width: 980px;
            margin: 0 auto;
            padding: 24px;
        }
        .top {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 16px;
        }
        h1 {
            margin: 0;
            font-size: clamp(1.3rem, 2.6vw, 1.9rem);
            letter-spacing: 0.3px;
        }
        .muted { color: var(--muted); font-size: 0.95rem; }
        .btn {
            background: var(--accent);
            color: #fff;
            border: 0;
            border-radius: 10px;
            padding: 10px 14px;
            cursor: pointer;
            box-shadow: var(--shadow);
        }
        .kpis {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 10px;
            margin-bottom: 12px;
        }
        .kpi {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 10px 12px;
            box-shadow: var(--shadow);
        }
        .kpi b { display: block; font-size: 1.2rem; margin-top: 4px; }
        .panel {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 14px;
            box-shadow: var(--shadow);
            padding: 12px;
        }
        .label { font-size: 12px; color: var(--muted); margin-bottom: 2px; }
        .mono {
            font-family: ui-monospace, Menlo, Consolas, monospace;
            word-break: break-all;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
            margin-top: 8px;
        }
        @media (max-width: 860px) {
            .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        }
        @media (max-width: 540px) {
            .kpis { grid-template-columns: 1fr; }
            .grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class=\"wrap\">
        <div class=\"top\">
            <div>
                <h1>Block Found Dashboard</h1>
                <div class=\"muted\">From translator_candidate_blocks</div>
            </div>
            <button id=\"refreshBtn\" class=\"btn\">Refresh</button>
        </div>

        <div class=\"kpis\" id=\"kpis\"></div>

        <section class=\"panel\" id=\"recentPanel\">Loading recent blocks...</section>
    </div>

    <script>
        function fmtNum(value) {
            const n = Number(value || 0);
            if (Number.isNaN(n)) return '-';
            return n.toLocaleString();
        }

        function fmtMst(value) {
            if (!value) return '-';
            const raw = String(value).trim();
            const hasTz = /([zZ]|[+-]\\d{2}:\\d{2})$/.test(raw);
            const utc = new Date(hasTz ? raw : `${raw}Z`);
            if (Number.isNaN(utc.getTime())) return value;
            const mst = new Date(utc.getTime() - 7 * 60 * 60 * 1000);
            return `${mst.toISOString().slice(0, 19).replace('T', ' ')} MST`;
        }

        function fmtInterval(seconds) {
            if (seconds === null || seconds === undefined) return '-';
            const s = Math.round(Number(seconds));
            if (Number.isNaN(s)) return '-';
            if (s < 60) return `${s}s`;
            const m = Math.floor(s / 60);
            const rem = s % 60;
            if (m < 60) return rem > 0 ? `${m}m ${rem}s` : `${m}m`;
            const h = Math.floor(m / 60);
            const remM = m % 60;
            return remM > 0 ? `${h}h ${remM}m` : `${h}h`;
        }

        function renderBlock(block, idx) {
            const label = idx === 0 ? 'Latest Found Block' : `Found Block #${idx + 1}`;
            return `
                <div style=\"margin-bottom:${idx < 2 ? '14px' : '0'};padding-bottom:${idx < 2 ? '14px' : '0'};border-bottom:${idx < 2 ? '1px solid var(--border)' : 'none'};\">
                    <div class=\"label\">${label}</div>
                    <div class=\"mono\" style=\"font-size:0.9rem;margin-bottom:6px;\">${block.blockhash || '-'}</div>
                    <div class=\"grid\">
                        <div>
                            <div class=\"label\">Found Time (MST)</div>
                            <div>${fmtMst(block.found_time)}</div>
                        </div>
                        <div>
                            <div class=\"label\">Channel</div>
                            <div>${block.channel_id ?? '-'}</div>
                        </div>
                        <div>
                            <div class=\"label\">Worker Identity</div>
                            <div class=\"mono\">${block.worker_identity || '-'}</div>
                        </div>
                        <div>
                            <div class=\"label\">Source / Proof Type</div>
                            <div>${block.source || '-'} / ${block.proof_type || '-'}</div>
                        </div>
                    </div>
                </div>
            `;
        }

        function render(data) {
            const kpis = document.getElementById('kpis');
            const recentPanel = document.getElementById('recentPanel');
            const counts = data.counts || {};
            const recent = data.recent_blocks || [];
            const avgSecs = data.avg_block_interval_seconds;
            const avgBasis = data.avg_block_interval_basis || 0;

            kpis.innerHTML = [
                ['Blocks in Last Hour', fmtNum(counts.last_1h || 0)],
                ['Blocks in Last 8 Hours', fmtNum(counts.last_8h || 0)],
                ['Blocks in Last 24 Hours', fmtNum(counts.last_24h || 0)],
                ['Avg Time Between Blocks', `${fmtInterval(avgSecs)}<span style=\"font-size:11px;color:var(--muted);font-weight:normal;\"> (last ${fmtNum(avgBasis)})</span>`],
            ].map(([k, v]) => `<div class=\"kpi\"><span class=\"muted\">${k}</span><b>${v}</b></div>`).join('');

            if (!recent.length) {
                recentPanel.innerHTML = '<div class=\"label\">Recent Found Blocks</div><div>No rows found yet.</div>';
                return;
            }

            recentPanel.innerHTML =
                recent.map((block, idx) => renderBlock(block, idx)).join('') +
                `<div class=\"muted\" style=\"margin-top:10px;\">Generated: ${fmtMst(data.generated_at)}</div>`;
        }

        async function loadData() {
            const res = await fetch('/audit/block-found');
            if (!res.ok) {
                document.getElementById('recentPanel').textContent = 'Failed to load block-found metrics.';
                document.getElementById('kpis').innerHTML = '';
                return;
            }
            const data = await res.json();
            render(data);
        }

        document.getElementById('refreshBtn').addEventListener('click', loadData);
        loadData();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


def _read_matured_window_data() -> dict[str, object]:
    settings = load_settings()
    repository = _get_postgres_candidate_read_repository()
    now = datetime.now(UTC)

    latest_window = repository.get_latest_settlement_window()
    if latest_window is None:
        return {
            "status": "no_data",
            "message": "No settlement windows found.",
            "generated_at": now.isoformat(),
        }

    settlement_id = int(latest_window["id"])
    settlement_blocks_rows = repository.list_settlement_blocks(settlement_id)
    block_hashes = [str(row["blockhash"]) for row in settlement_blocks_rows if row.get("blockhash")]

    stored_rewards: dict[str, dict[str, Any]] = {}
    for bh in block_hashes:
        reward_row = repository.get_block_reward(bh)
        if reward_row is not None:
            stored_rewards[bh] = reward_row

    live_rewards: dict[str, int] = {}
    live_api_status = "ok"
    live_api_error: str | None = None
    try:
        if block_hashes:
            live_rewards = fetch_block_rewards_by_hashes(block_hashes)
    except PoolApiError as exc:
        live_api_status = "error"
        live_api_error = str(exc)
    except Exception as exc:
        live_api_status = "error"
        live_api_error = f"Unexpected error: {exc}"

    blocks: list[dict[str, object]] = []
    total_stored_reward_sats = 0
    total_live_reward_sats = 0
    mismatch_count = 0

    for row in settlement_blocks_rows:
        bh = str(row.get("blockhash") or "")
        settlement_reward_sats = int(row.get("reward_sats") or 0)

        reward_row = stored_rewards.get(bh)
        stored_reward_sats: int | None = int(reward_row["reward_sats"]) if reward_row else None
        stored_fetched_at = reward_row.get("fetched_at") if reward_row else None

        live_reward_sats: int | None = live_rewards.get(bh) if live_api_status == "ok" else None

        if live_api_status == "error":
            block_status = "api_error"
        elif live_reward_sats is None or live_reward_sats <= 0:
            block_status = "missing"
        elif stored_reward_sats is not None and live_reward_sats != stored_reward_sats:
            block_status = "mismatch"
            mismatch_count += 1
        else:
            block_status = "confirmed"

        if stored_reward_sats:
            total_stored_reward_sats += stored_reward_sats
        if live_reward_sats:
            total_live_reward_sats += live_reward_sats

        found_at = row.get("found_at")
        blocks.append({
            "blockhash": bh,
            "found_at": found_at.isoformat() if isinstance(found_at, datetime) else None,
            "channel_id": _to_int(row.get("channel_id")),
            "worker_identity": row.get("worker_identity"),
            "source": row.get("source"),
            "settlement_reward_sats": settlement_reward_sats,
            "stored_reward_sats": stored_reward_sats,
            "stored_fetched_at": (
                stored_fetched_at.isoformat() if isinstance(stored_fetched_at, datetime) else None
            ),
            "live_reward_sats": live_reward_sats,
            "status": block_status,
        })

    work_window_start = latest_window.get("work_window_start")
    work_window_end = latest_window.get("work_window_end")
    settlement_run_at = latest_window.get("settlement_run_at")

    return {
        "status": "ok",
        "generated_at": now.isoformat(),
        "live_api_status": live_api_status,
        "live_api_error": live_api_error,
        "settlement": {
            "id": settlement_id,
            "status": latest_window.get("status"),
            "work_window_start": (
                work_window_start.isoformat() if isinstance(work_window_start, datetime) else None
            ),
            "work_window_end": (
                work_window_end.isoformat() if isinstance(work_window_end, datetime) else None
            ),
            "maturity_offset_minutes": latest_window.get("maturity_offset_minutes"),
            "total_reward_sats": int(latest_window.get("total_reward_sats") or 0),
            "settlement_run_at": (
                settlement_run_at.isoformat() if isinstance(settlement_run_at, datetime) else None
            ),
        },
        "block_count": len(blocks),
        "total_stored_reward_sats": total_stored_reward_sats,
        "total_live_reward_sats": total_live_reward_sats if live_api_status == "ok" else None,
        "mismatch_count": mismatch_count,
        "blocks": blocks,
    }


@app.get("/audit/matured-window")
def audit_matured_window() -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=_read_matured_window_data())
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "error": "Unable to read matured window data.",
                "details": str(exc),
            },
        )


@app.get("/audit/matured-window/dashboard", response_class=HTMLResponse)
def audit_matured_window_dashboard() -> HTMLResponse:
    html = """<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Matured Window Dashboard</title>
    <style>
        :root {
            --bg0: #f4f1ea;
            --bg1: #e8dcc8;
            --panel: #fffdfa;
            --ink: #1f1a15;
            --muted: #6a5c4d;
            --border: #d7c8b3;
            --accent: #0f5c78;
            --shadow: 0 10px 30px rgba(34, 26, 15, 0.12);
            --ok: #1a7a3f;
            --ok-bg: #e6f4ec;
            --warn: #7a5c1a;
            --warn-bg: #fef6e4;
            --err: #7a1a1a;
            --err-bg: #fde8e8;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            color: var(--ink);
            font-family: Georgia, "Times New Roman", serif;
            background:
                radial-gradient(1300px 700px at -10% -20%, #f8edd6 10%, transparent 60%),
                radial-gradient(900px 500px at 120% -10%, #d2e8ef 10%, transparent 55%),
                linear-gradient(180deg, var(--bg0), var(--bg1));
            min-height: 100vh;
        }
        .wrap { max-width: 1100px; margin: 0 auto; padding: 24px; }
        .top {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 16px;
        }
        h1 { margin: 0; font-size: clamp(1.3rem, 2.6vw, 1.9rem); letter-spacing: 0.3px; }
        .muted { color: var(--muted); font-size: 0.95rem; }
        .btn {
            background: var(--accent);
            color: #fff;
            border: 0;
            border-radius: 10px;
            padding: 10px 14px;
            cursor: pointer;
            box-shadow: var(--shadow);
        }
        .kpis {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 10px;
            margin-bottom: 12px;
        }
        .kpi {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 10px 12px;
            box-shadow: var(--shadow);
        }
        .kpi b { display: block; font-size: 1.2rem; margin-top: 4px; }
        .panel {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 14px;
            box-shadow: var(--shadow);
            padding: 16px;
        }
        .panel-title {
            font-size: 1rem;
            font-weight: bold;
            margin-bottom: 12px;
        }
        .label { font-size: 12px; color: var(--muted); margin-bottom: 2px; }
        .mono {
            font-family: ui-monospace, Menlo, Consolas, monospace;
            word-break: break-all;
        }
        .badge {
            display: inline-block;
            border-radius: 6px;
            padding: 2px 8px;
            font-size: 0.82rem;
            font-family: ui-monospace, Menlo, Consolas, monospace;
            font-weight: bold;
        }
        .badge-confirmed { background: var(--ok-bg); color: var(--ok); }
        .badge-missing { background: var(--err-bg); color: var(--err); }
        .badge-mismatch { background: var(--warn-bg); color: var(--warn); }
        .badge-api_error { background: var(--err-bg); color: var(--err); }
        .block-row {
            border-bottom: 1px solid var(--border);
            padding: 14px 0;
        }
        .block-row:last-child { border-bottom: none; padding-bottom: 0; }
        .block-header {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 8px;
        }
        .block-num {
            font-size: 0.8rem;
            color: var(--muted);
            min-width: 28px;
        }
        .block-hash {
            font-family: ui-monospace, Menlo, Consolas, monospace;
            font-size: 0.85rem;
            word-break: break-all;
            flex: 1;
        }
        .block-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 8px 12px;
        }
        .reward-cell { }
        .reward-val { font-size: 1rem; font-weight: bold; }
        .reward-btc { font-size: 0.78rem; color: var(--muted); }
        .window-info {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 10px;
            margin-bottom: 14px;
        }
        .info-card {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 10px 12px;
            box-shadow: var(--shadow);
        }
        @media (max-width: 900px) {
            .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .window-info { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .block-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        }
        @media (max-width: 540px) {
            .kpis { grid-template-columns: 1fr; }
            .window-info { grid-template-columns: 1fr; }
            .block-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class="wrap">
        <div class="top">
            <div>
                <h1>Matured Window Dashboard</h1>
                <div class="muted" id="subtitle">Last processed settlement window</div>
            </div>
            <button id="refreshBtn" class="btn">Refresh</button>
        </div>

        <div class="window-info" id="windowInfo"></div>
        <div class="kpis" id="kpis"></div>
        <section class="panel" id="blocksPanel">Loading...</section>
    </div>

    <script>
        const SATS_PER_BTC = 100_000_000;

        function fmtMst(value) {
            if (!value) return '-';
            const raw = String(value).trim();
            const hasTz = /([zZ]|[+-]\\d{2}:\\d{2})$/.test(raw);
            const utc = new Date(hasTz ? raw : raw + 'Z');
            if (Number.isNaN(utc.getTime())) return value;
            const mst = new Date(utc.getTime() - 7 * 60 * 60 * 1000);
            return mst.toISOString().slice(0, 19).replace('T', ' ') + ' MST';
        }

        function fmtSats(sats) {
            if (sats === null || sats === undefined) return '-';
            const n = Number(sats);
            if (Number.isNaN(n)) return '-';
            const btc = (n / SATS_PER_BTC).toFixed(8);
            return `${n.toLocaleString()} sats`;
        }

        function fmtBtc(sats) {
            if (sats === null || sats === undefined) return '';
            const n = Number(sats);
            if (Number.isNaN(n)) return '';
            return (n / SATS_PER_BTC).toFixed(8) + ' BTC';
        }

        function badge(status) {
            const labels = {
                confirmed: '✓ confirmed',
                missing: '✗ missing',
                mismatch: '⚠ mismatch',
                api_error: '✗ api error',
            };
            return `<span class="badge badge-${status}">${labels[status] || status}</span>`;
        }

        function renderBlock(block, idx) {
            const rewardFields = [
                { label: 'Settlement Reward', sats: block.settlement_reward_sats },
                { label: 'Stored Reward (cache)', sats: block.stored_reward_sats },
                { label: 'Live Reward (API)', sats: block.live_reward_sats },
            ];
            const rewardCells = rewardFields.map(f => `
                <div class="reward-cell">
                    <div class="label">${f.label}</div>
                    <div class="reward-val">${fmtSats(f.sats)}</div>
                    <div class="reward-btc">${fmtBtc(f.sats)}</div>
                </div>
            `).join('');

            return `
                <div class="block-row">
                    <div class="block-header">
                        <span class="block-num">#${idx + 1}</span>
                        <span class="block-hash">${block.blockhash || '-'}</span>
                        ${badge(block.status)}
                    </div>
                    <div class="block-grid">
                        <div>
                            <div class="label">Found At (MST)</div>
                            <div>${fmtMst(block.found_at)}</div>
                        </div>
                        <div>
                            <div class="label">Channel</div>
                            <div>${block.channel_id ?? '-'}</div>
                        </div>
                        <div>
                            <div class="label">Worker Identity</div>
                            <div class="mono" style="font-size:0.85rem;">${block.worker_identity || '-'}</div>
                        </div>
                        ${rewardCells}
                        ${block.stored_fetched_at ? `
                        <div>
                            <div class="label">Reward Cached At (MST)</div>
                            <div>${fmtMst(block.stored_fetched_at)}</div>
                        </div>` : ''}
                        <div>
                            <div class="label">Source</div>
                            <div>${block.source || '-'}</div>
                        </div>
                    </div>
                </div>
            `;
        }

        function render(data) {
            const windowInfo = document.getElementById('windowInfo');
            const kpis = document.getElementById('kpis');
            const blocksPanel = document.getElementById('blocksPanel');
            const subtitle = document.getElementById('subtitle');

            if (data.status === 'no_data') {
                windowInfo.innerHTML = '';
                kpis.innerHTML = '';
                blocksPanel.innerHTML = '<div class="label">No settlement windows found yet.</div>';
                return;
            }

            const s = data.settlement || {};
            subtitle.textContent = `Settlement #${s.id || '?'} · status: ${s.status || '?'}`;

            const liveStatus = data.live_api_status === 'ok'
                ? '<span style="color:var(--ok);font-weight:bold;">✓ live API ok</span>'
                : `<span style="color:var(--err);font-weight:bold;">✗ API error</span>`;

            windowInfo.innerHTML = [
                ['Window Start', fmtMst(s.work_window_start)],
                ['Window End', fmtMst(s.work_window_end)],
                [`Maturity Offset`, `${s.maturity_offset_minutes ?? '-'} min`],
                ['Settlement Run At', fmtMst(s.settlement_run_at)],
                ['Live API Status', liveStatus],
                data.live_api_error ? ['API Error', `<span style="color:var(--err);font-size:0.85rem;">${data.live_api_error}</span>`] : null,
            ].filter(Boolean).map(([k, v]) => `
                <div class="info-card">
                    <div class="label">${k}</div>
                    <div>${v}</div>
                </div>
            `).join('');

            const totalLive = data.total_live_reward_sats !== null && data.total_live_reward_sats !== undefined
                ? `${fmtSats(data.total_live_reward_sats)}<div style="font-size:0.75rem;color:var(--muted);font-weight:normal;">${fmtBtc(data.total_live_reward_sats)}</div>`
                : '<span style="color:var(--muted);">N/A</span>';

            kpis.innerHTML = [
                ['Blocks in Window', String(data.block_count || 0)],
                ['Stored Total Reward', `${fmtSats(data.total_stored_reward_sats)}<div style="font-size:0.75rem;color:var(--muted);font-weight:normal;">${fmtBtc(data.total_stored_reward_sats)}</div>`],
                ['Live Total Reward', totalLive],
                ['Mismatches', String(data.mismatch_count || 0)],
            ].map(([k, v]) => `<div class="kpi"><span class="muted">${k}</span><b>${v}</b></div>`).join('');

            const blocks = data.blocks || [];
            if (!blocks.length) {
                blocksPanel.innerHTML = '<div class="panel-title">Blocks in Window</div><div>No blocks found in this settlement window.</div>';
                return;
            }

            blocksPanel.innerHTML =
                `<div class="panel-title">Blocks in Window (${blocks.length})</div>` +
                blocks.map((b, i) => renderBlock(b, i)).join('') +
                `<div class="muted" style="margin-top:12px;font-size:0.88rem;">Generated: ${fmtMst(data.generated_at)}</div>`;
        }

        async function loadData() {
            document.getElementById('blocksPanel').textContent = 'Loading...';
            try {
                const res = await fetch('/audit/matured-window');
                if (!res.ok) {
                    document.getElementById('blocksPanel').textContent = 'Failed to load matured window data.';
                    return;
                }
                const data = await res.json();
                render(data);
            } catch (e) {
                document.getElementById('blocksPanel').textContent = 'Error: ' + e.message;
            }
        }

        document.getElementById('refreshBtn').addEventListener('click', loadData);
        loadData();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@app.get("/postgres-shadow/settlements/{settlement_id}/compare")
@app.get("/v1/postgres-shadow/settlements/{settlement_id}/compare")
def compare_postgres_shadow_endpoint(settlement_id: int) -> JSONResponse:
    _settings = load_settings()
    if _settings.postgres_primary_session_enabled:
        payload, status_code = compare_postgres_shadow_settlement(None, settlement_id)
    else:
        with _new_session() as session:
            payload, status_code = compare_postgres_shadow_settlement(session, settlement_id)
    return JSONResponse(status_code=status_code, content=payload)


@app.get("/postgres-shadow/settlements/audit")
@app.get("/v1/postgres-shadow/settlements/audit")
def audit_postgres_shadow_endpoint(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, pattern="^(matched|mismatched|not_found|error)$"),
    include_details: bool = False,
) -> JSONResponse:
    _settings = load_settings()
    if _settings.postgres_primary_session_enabled:
        payload, status_code = audit_postgres_shadow_settlements(
            None,
            limit=limit,
            offset=offset,
            status_filter=status_filter,
            include_details=include_details,
        )
    else:
        with _new_session() as session:
            payload, status_code = audit_postgres_shadow_settlements(
                session,
                limit=limit,
                offset=offset,
                status_filter=status_filter,
                include_details=include_details,
            )
    return JSONResponse(status_code=status_code, content=payload)


@app.get("/postgres-shadow/read-mode")
@app.get("/v1/postgres-shadow/read-mode")
def postgres_shadow_read_mode_endpoint() -> dict[str, object]:
    settings = load_settings()
    return {
        "configured_mode": settings.postgres_ledger_read_mode,
        "effective_mode": settings.effective_postgres_read_mode,
        "reads_enabled": settings.postgres_ledger_reads_enabled,
        "fallback_enabled": settings.postgres_ledger_read_fallback_to_sqlite,
        "require_shadow_match": settings.postgres_ledger_read_require_shadow_match,
        "allowed_endpoints": list(settings.postgres_ledger_read_allowed_endpoints),
    }


@app.post("/settlements/run")
def run_settlement_cycle() -> dict:
    return _execute_settlement_cycle(force_settlement=True)


def _execute_settlement_cycle(*, force_settlement: bool = False) -> dict:
    settings = load_settings()
    reward_mode = _normalize_reward_mode(settings.reward_mode)
    block_reward_btc = Decimal(str(settings.block_reward_btc or "1.87500000"))
    attempt_id = str(uuid.uuid4())
    postgres_shadow_write: dict[str, object] | None = None
    postgres_polling_repository: PostgresLedgerRepository | None = None
    requires_runtime_postgres_repository = (
        settings.postgres_ledger_shadow_write_enabled
        or settings.postgres_settlement_engine_enabled
        or settings.postgres_sender_enabled
    )

    if requires_runtime_postgres_repository:
        try:
            postgres_polling_repository = PostgresLedgerRepository(
                make_postgres_session_factory(make_postgres_engine())
            )
        except Exception as exc:
            postgres_polling_repository = None
            _write_scheduler_event(
                "postgres_polling_shadow_writer_init_failed",
                {
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )
            if settings.sqlite_retirement_mode_enabled:
                raise RuntimeError(
                    "SQLite retirement mode is enabled and runtime Postgres repository initialization failed."
                ) from exc

    with _new_session() as session:
        attempt_time = datetime.now(UTC).replace(tzinfo=None)
        interval_minutes = int(settings.payout_interval_minutes)
        interval_delta = timedelta(minutes=interval_minutes)
        latest_settlement = None
        latest_settlement_period_end: datetime | None = None

        if settings.postgres_primary_session_enabled and postgres_polling_repository is not None:
            latest_settlement = postgres_polling_repository.get_latest_settlement_window()
            if latest_settlement is not None:
                work_window_end = latest_settlement.get("work_window_end")
                if isinstance(work_window_end, datetime):
                    if work_window_end.tzinfo is None or work_window_end.utcoffset() is None:
                        latest_settlement_period_end = work_window_end
                    else:
                        latest_settlement_period_end = work_window_end.astimezone(UTC).replace(tzinfo=None)
        else:
            latest_settlement = session.execute(
                select(Settlement).order_by(Settlement.period_end.desc(), Settlement.id.desc()).limit(1)
            ).scalar_one_or_none()
            if latest_settlement is not None:
                latest_settlement_period_end = latest_settlement.period_end

        next_settlement_due_at = None
        if latest_settlement_period_end is not None:
            next_settlement_due_at = latest_settlement_period_end + interval_delta
        elif _SERVICE_STARTED_AT is not None:
            next_settlement_due_at = _SERVICE_STARTED_AT + interval_delta

        block_reward_payload: dict[str, object] | None = None
        interval_blocks = 0
        block_delta_details: list[dict[str, int | bool]] = []

        reward_fetcher = None
        selected_snapshot_block_ids: list[int] = []
        selected_matured_hashes: list[str] = []
        _pg_blocks_mode: bool = False
        _pg_matured_blocks: list[dict] = []
        _pg_block_repo: Any | None = None

        if settings.translator_channels_url:
            snapshots_created, current_blocks_found_by_channel = poll_channels_once_with_blocks(
                session,
                settings.translator_channels_url,
                downstream_url=settings.translator_downstreams_url,
                bearer_token=settings.translator_bearer_token,
                raw_snapshot_writer=postgres_polling_repository,
                sqlite_write_enabled=(
                    bool(settings.sqlite_runtime_writes_enabled)
                    and not bool(settings.postgres_primary_session_enabled)
                ),
            )
            if reward_mode == "blocks":
                interval_blocks, block_delta_details = _compute_interval_blocks_delta(
                    session,
                    current_blocks_found_by_channel,
                    settings=settings,
                    postgres_repository=postgres_polling_repository,
                )
                computed_reward = Decimal(interval_blocks) * block_reward_btc
                block_reward_payload = {
                    "reward_mode": "blocks",
                    "block_reward_btc": _to_decimal_str(block_reward_btc),
                    "interval_blocks": int(interval_blocks),
                    "computed_reward_btc": _to_decimal_str(computed_reward),
                    "channels": block_delta_details,
                }
                reward_fetcher = lambda _start, _end: computed_reward
        else:
            snapshots_created = poll_metrics_once(
                session,
                settings.translator_metrics_url,
                raw_snapshot_writer=postgres_polling_repository,
                sqlite_write_enabled=(
                    bool(settings.sqlite_runtime_writes_enabled)
                    and not bool(settings.postgres_primary_session_enabled)
                ),
            )

        should_settle = force_settlement
        if not should_settle:
            due_time_tolerance = timedelta(seconds=1)
            should_settle = (
                next_settlement_due_at is None
                or attempt_time + due_time_tolerance >= next_settlement_due_at
            )

        if not should_settle:
            if settings.postgres_sender_enabled:
                if postgres_polling_repository is None:
                    if settings.sqlite_retirement_mode_enabled:
                        raise RuntimeError(
                            "SQLite retirement mode is enabled but Postgres sender is unavailable."
                        )
                    sender_stats = process_payout_events(session, dry_run=settings.dry_run)
                else:
                    sender_stats = process_payout_events_postgres(
                        postgres_polling_repository,
                        dry_run=settings.dry_run,
                    )
            else:
                if settings.sqlite_retirement_mode_enabled:
                    raise RuntimeError(
                        "SQLite retirement mode is enabled; set POSTGRES_SENDER_ENABLED=true."
                    )
                sender_stats = process_payout_events(session, dry_run=settings.dry_run)
            response: dict[str, object] = {
                "snapshots_created": snapshots_created,
                "settlement": None,
                "settlement_skipped": True,
                "skip_reason": "payout_interval_not_elapsed",
                "next_settlement_due_at": next_settlement_due_at.isoformat()
                if next_settlement_due_at
                else None,
                "sender": {
                    "attempted": sender_stats.attempted,
                    "sent": sender_stats.sent,
                    "failed": sender_stats.failed,
                    "created_events": sender_stats.created_events,
                },
            }
            if settings.translator_channels_url and reward_mode == "blocks":
                response["block_reward"] = {
                    "reward_mode": "blocks",
                    "block_reward_btc": _to_decimal_str(block_reward_btc),
                    "interval_blocks": int(interval_blocks),
                    "computed_reward_btc": _to_decimal_str(Decimal(interval_blocks) * block_reward_btc),
                    "channels": block_delta_details,
                }
            return response

        if settings.enable_block_event_rewards:
            matured_start, matured_end = compute_matured_window(
                attempt_time,
                interval_minutes=interval_minutes,
                maturity_window_minutes=int(settings.maturity_window_minutes),
            )
            fetched_block_rows: list[dict[str, object]] = []
            try:
                fetched_block_rows = fetch_blocks_found_in_window(matured_start, matured_end)
            except PoolApiError:
                fetched_block_rows = []

            if settings.enable_block_event_replay_hook:
                try:
                    replay_rows = run_block_event_replay_hook(session, matured_start, matured_end)
                except Exception:
                    replay_rows = []
                if replay_rows:
                    fetched_block_rows.extend(replay_rows)

            # Upsert blocks to Postgres or SQLite based on primary session mode
            if getattr(settings, "postgres_primary_session_enabled", False):
                try:
                    postgres_repo = PostgresLedgerRepository(
                        make_postgres_session_factory(make_postgres_engine())
                    )
                    inserted_blocks = upsert_blocks_found_postgres(
                        postgres_repo,
                        fetched_block_rows,
                        source_default="translator_blocks_api",
                    )
                    # Bridge translator_candidate_blocks → blocks_found.
                    # The capture proxy writes candidate blocks directly to Postgres but the
                    # settlement engine only reads from blocks_found, so we promote any
                    # candidate blocks in the matured window that are not already present.
                    _matured_start_aware = matured_start if matured_start.tzinfo is not None else matured_start.replace(tzinfo=UTC)
                    _matured_end_aware = matured_end if matured_end.tzinfo is not None else matured_end.replace(tzinfo=UTC)
                    try:
                        candidate_rows = postgres_repo.list_translator_candidate_blocks(
                            start_time=_matured_start_aware,
                            end_time=_matured_end_aware,
                            limit=500,
                            order="asc",
                        )
                        for _cand in candidate_rows:
                            try:
                                postgres_repo.upsert_block_found(
                                    blockhash=str(_cand["blockhash"]),
                                    found_at=_cand["found_time"],
                                    channel_id=_cand.get("channel_id"),
                                    worker_identity=_cand.get("worker_identity"),
                                    source="translator_candidate_blocks",
                                    created_at=datetime.now(UTC),
                                )
                            except Exception:
                                pass  # non-fatal; skip duplicate or invalid rows
                    except Exception:
                        pass  # non-fatal; settlement still runs on API-fetched blocks
                except Exception:
                    # Fall back to SQLite if Postgres fails
                    inserted_blocks = upsert_snapshot_blocks(
                        session,
                        fetched_block_rows,
                        source_default="translator_blocks_api",
                    )
            else:
                inserted_blocks = upsert_snapshot_blocks(
                    session,
                    fetched_block_rows,
                    source_default="translator_blocks_api",
                )
            # --- Block read: Postgres-primary or SQLite fallback ---
            _use_pg_blocks = bool(getattr(settings, "postgres_primary_session_enabled", False))
            if _use_pg_blocks:
                try:
                    _pg_block_repo = PostgresLedgerRepository(
                        make_postgres_session_factory(make_postgres_engine())
                    )
                    _start_aware = matured_start if matured_start.tzinfo is not None else matured_start.replace(tzinfo=UTC)
                    _end_aware = matured_end if matured_end.tzinfo is not None else matured_end.replace(tzinfo=UTC)
                    _pg_matured_dicts = _pg_block_repo.list_matured_blocks_in_window(_start_aware, _end_aware)
                    _pg_retry_dicts = _pg_block_repo.list_retry_blocks(_end_aware)
                except Exception:
                    if should_fail_closed_on_postgres_primary(
                        postgres_primary_session_enabled=settings.postgres_primary_session_enabled,
                        sqlite_retirement_mode_enabled=settings.sqlite_retirement_mode_enabled,
                    ):
                        raise RuntimeError(
                            "Postgres block read failed while Postgres primary is enabled."
                        )
                    _use_pg_blocks = False

            if _use_pg_blocks:
                _pg_blocks_mode = True
                _rows_by_hash_pg: dict[str, dict] = {}
                for _r in _pg_matured_dicts:
                    _rows_by_hash_pg[str(_r["blockhash"])] = dict(_r)
                for _r in _pg_retry_dicts:
                    _bh = str(_r["blockhash"])
                    if _bh not in _rows_by_hash_pg:
                        _rows_by_hash_pg[_bh] = dict(_r)
                _selected_pg = list(_rows_by_hash_pg.values())
                current_matured_hashes = [str(_r["blockhash"]) for _r in _pg_matured_dicts if _r.get("blockhash")]
                current_matured_hash_set = set(current_matured_hashes)
                retry_hashes = [
                    str(_r["blockhash"])
                    for _r in _selected_pg
                    if _r.get("blockhash") and str(_r["blockhash"]) not in current_matured_hash_set
                ]
                selected_snapshot_block_ids = []  # not used in Postgres path
                _pg_matured_blocks = [dict(_r) for _r in _pg_matured_dicts]
                selected_matured_hashes = [str(_r["blockhash"]) for _r in _selected_pg if _r.get("blockhash")]

                rewards_sats_by_hash: dict[str, int] = {}
                if selected_matured_hashes:
                    try:
                        rewards_sats_by_hash = fetch_block_rewards_by_hashes(selected_matured_hashes)
                    except PoolApiError:
                        rewards_sats_by_hash = {}

                if settings.enable_reward_refetch_hook and selected_matured_hashes:
                    try:
                        rewards_sats_by_hash = run_reward_refetch_hook(
                            selected_matured_hashes,
                            rewards_sats_by_hash,
                        )
                    except Exception:
                        pass

                missing_reward_hashes = [
                    bh for bh in selected_matured_hashes if bh not in rewards_sats_by_hash
                ]
                missing_current_matured_hashes = [
                    bh for bh in current_matured_hashes if bh not in rewards_sats_by_hash
                ]
                reward_entries_complete = not missing_current_matured_hashes

                now_utc_aware = datetime.now(UTC)
                total_sats = 0
                for _r in _selected_pg:
                    sats = int(rewards_sats_by_hash.get(str(_r["blockhash"]), 0) or 0)
                    total_sats += sats
                    _r["reward_sats"] = sats
                    if sats > 0:
                        try:
                            _pg_block_repo.upsert_block_reward(
                                blockhash=str(_r["blockhash"]),
                                reward_sats=sats,
                                fetched_at=now_utc_aware,
                            )
                        except Exception:
                            pass  # non-fatal; reward counted in total_sats regardless
                # Also update pg_matured_blocks with resolved reward_sats for linking
                for _r in _pg_matured_blocks:
                    _r["reward_sats"] = int(rewards_sats_by_hash.get(str(_r["blockhash"]), 0) or 0)

                computed_reward = Decimal(total_sats) / Decimal("100000000")
                settlement_reward = computed_reward if reward_entries_complete else Decimal("0")
                reward_fetcher = lambda _start, _end: settlement_reward
                block_reward_payload = {
                    "reward_mode": "block_events",
                    "matured_window_start": matured_start.isoformat(),
                    "matured_window_end": matured_end.isoformat(),
                    "fetched_block_count": len(fetched_block_rows),
                    "inserted_block_count": int(inserted_blocks),
                    "interval_blocks": len(current_matured_hashes),
                    "matured_hash_count": len([_r for _r in _pg_matured_dicts if _r.get("blockhash")]),
                    "retry_hash_count": len(selected_matured_hashes),
                    "retry_only_hash_count": len(retry_hashes),
                    "computed_reward_btc": _to_decimal_str(computed_reward),
                    "settlement_reward_btc": _to_decimal_str(settlement_reward),
                    "reward_entries_complete": reward_entries_complete,
                    "missing_reward_hash_count": len(missing_reward_hashes),
                    "missing_current_matured_hash_count": len(missing_current_matured_hashes),
                }
                # No session.flush() needed — rewards persisted via upsert_block_reward calls above
            else:
                # SQLite fallback: read matured/retry blocks from SnapshotBlock
                matured_rows = session.execute(
                    select(SnapshotBlock)
                    .where(
                        SnapshotBlock.found_at >= matured_start,
                        SnapshotBlock.found_at < matured_end,
                        SnapshotBlock.settlement_id.is_(None),
                    )
                    .order_by(SnapshotBlock.found_at.asc(), SnapshotBlock.id.asc())
                ).scalars().all()

                # Retry previously seen block hashes that still have no resolved reward,
                # even if their found_at is outside the current matured window.
                retry_rows = session.execute(
                    select(SnapshotBlock)
                    .where(
                        SnapshotBlock.settlement_id.is_not(None),
                        SnapshotBlock.found_at < matured_end,
                        or_(
                            SnapshotBlock.reward_sats.is_(None),
                            SnapshotBlock.reward_sats <= 0,
                        ),
                    )
                    .order_by(SnapshotBlock.found_at.asc(), SnapshotBlock.id.asc())
                    .limit(1000)
                ).scalars().all()

                rows_by_hash: dict[str, SnapshotBlock] = {}
                for row in matured_rows:
                    rows_by_hash[row.blockhash] = row
                for row in retry_rows:
                    if row.blockhash not in rows_by_hash:
                        rows_by_hash[row.blockhash] = row

                selected_rows = list(rows_by_hash.values())
                current_matured_hashes = [row.blockhash for row in matured_rows if row.blockhash]
                current_matured_hash_set = set(current_matured_hashes)
                retry_hashes = [
                    row.blockhash
                    for row in selected_rows
                    if row.blockhash and row.blockhash not in current_matured_hash_set
                ]

                selected_snapshot_block_ids = [int(row.id) for row in matured_rows]
                selected_matured_hashes = [row.blockhash for row in selected_rows if row.blockhash]

                rewards_sats_by_hash: dict[str, int] = {}
                if selected_matured_hashes:
                    try:
                        rewards_sats_by_hash = fetch_block_rewards_by_hashes(selected_matured_hashes)
                    except PoolApiError:
                        rewards_sats_by_hash = {}

                if settings.enable_reward_refetch_hook and selected_matured_hashes:
                    try:
                        rewards_sats_by_hash = run_reward_refetch_hook(
                            selected_matured_hashes,
                            rewards_sats_by_hash,
                        )
                    except Exception:
                        pass

                missing_reward_hashes = [
                    blockhash
                    for blockhash in selected_matured_hashes
                    if blockhash not in rewards_sats_by_hash
                ]
                missing_current_matured_hashes = [
                    blockhash
                    for blockhash in current_matured_hashes
                    if blockhash not in rewards_sats_by_hash
                ]
                reward_entries_complete = not missing_current_matured_hashes

                total_sats = 0
                now_utc_naive = datetime.now(UTC).replace(tzinfo=None)
                for row in selected_rows:
                    sats = int(rewards_sats_by_hash.get(row.blockhash, 0) or 0)
                    total_sats += sats
                    row.reward_sats = sats
                    row.reward_fetched_at = now_utc_naive
                    row.updated_at = now_utc_naive

                computed_reward = Decimal(total_sats) / Decimal("100000000")
                settlement_reward = computed_reward if reward_entries_complete else Decimal("0")
                reward_fetcher = lambda _start, _end: settlement_reward
                block_reward_payload = {
                    "reward_mode": "block_events",
                    "matured_window_start": matured_start.isoformat(),
                    "matured_window_end": matured_end.isoformat(),
                    "fetched_block_count": len(fetched_block_rows),
                    "inserted_block_count": int(inserted_blocks),
                    "interval_blocks": len(current_matured_hashes),
                    "matured_hash_count": len([row for row in matured_rows if row.blockhash]),
                    "retry_hash_count": len(selected_matured_hashes),
                    "retry_only_hash_count": len(retry_hashes),
                    "computed_reward_btc": _to_decimal_str(computed_reward),
                    "settlement_reward_btc": _to_decimal_str(settlement_reward),
                    "reward_entries_complete": reward_entries_complete,
                    "missing_reward_hash_count": len(missing_reward_hashes),
                    "missing_current_matured_hash_count": len(missing_current_matured_hashes),
                }

                session.flush()

        settlement_kwargs = {
            "interval_minutes": settings.payout_interval_minutes,
            "payout_decimals": settings.payout_decimals,
        }
        if reward_fetcher is not None:
            settlement_kwargs["reward_fetcher"] = reward_fetcher
        if settings.enable_block_event_rewards:
            settlement_kwargs["defer_on_zero_reward"] = bool(settings.defer_on_zero_matured_reward)
            settlement_kwargs["use_work_accrual"] = True
            settlement_kwargs["work_window_start"] = matured_start
            settlement_kwargs["work_window_end"] = matured_end

        settlement_engine = "sqlite"
        if settings.postgres_settlement_engine_enabled:
            if postgres_polling_repository is None:
                if settings.sqlite_retirement_mode_enabled:
                    raise RuntimeError(
                        "SQLite retirement mode is enabled but Postgres settlement engine repository is unavailable."
                    )
                settlement_result = run_settlement(
                    session,
                    attempt_time,
                    **settlement_kwargs,
                )
            else:
                try:
                    settlement_result = run_settlement_postgres(
                        postgres_polling_repository,
                        attempt_time,
                        **settlement_kwargs,
                    )
                    settlement_engine = "postgres"
                except Exception as exc:
                    if should_fail_closed_on_postgres_primary(
                        postgres_primary_session_enabled=settings.postgres_primary_session_enabled,
                        sqlite_retirement_mode_enabled=settings.sqlite_retirement_mode_enabled,
                    ):
                        raise RuntimeError(
                            "Postgres settlement engine failed while Postgres primary session or SQLite retirement mode is enabled."
                        ) from exc
                    settlement_engine = "sqlite_fallback"
                    _write_scheduler_event(
                        "postgres_settlement_engine_failed",
                        {
                            "error": str(exc),
                            "traceback": traceback.format_exc(),
                        },
                    )
                    settlement_result = run_settlement(
                        session,
                        attempt_time,
                        **settlement_kwargs,
                    )
        else:
            if settings.sqlite_retirement_mode_enabled:
                raise RuntimeError(
                    "SQLite retirement mode is enabled; set POSTGRES_SETTLEMENT_ENGINE_ENABLED=true."
                )
            settlement_result = run_settlement(
                session,
                attempt_time,
                **settlement_kwargs,
            )

        if _pg_blocks_mode and _pg_matured_blocks:
            # Postgres path: link matured blocks to the settlement window via settlement_blocks table
            try:
                _pg_block_repo.bulk_link_settlement_blocks(
                    settlement_result.settlement_id,
                    _pg_matured_blocks,
                )
            except Exception as _link_exc:
                if should_fail_closed_on_postgres_primary(
                    postgres_primary_session_enabled=settings.postgres_primary_session_enabled,
                    sqlite_retirement_mode_enabled=settings.sqlite_retirement_mode_enabled,
                ):
                    raise RuntimeError(
                        "Postgres settlement block linking failed while Postgres primary is enabled."
                    ) from _link_exc
        elif selected_snapshot_block_ids:
            rows_to_link = session.execute(
                select(SnapshotBlock).where(SnapshotBlock.id.in_(selected_snapshot_block_ids))
            ).scalars().all()
            now_utc_naive = datetime.now(UTC).replace(tzinfo=None)
            for row in rows_to_link:
                row.settlement_id = settlement_result.settlement_id
                row.updated_at = now_utc_naive
            session.flush()

        if settings.postgres_sender_enabled:
            if postgres_polling_repository is None:
                if settings.sqlite_retirement_mode_enabled:
                    raise RuntimeError(
                        "SQLite retirement mode is enabled but Postgres sender is unavailable."
                    )
                sender_stats = process_payout_events(session, dry_run=settings.dry_run)
            else:
                sender_stats = process_payout_events_postgres(
                    postgres_polling_repository,
                    dry_run=settings.dry_run,
                )
        else:
            if settings.sqlite_retirement_mode_enabled:
                raise RuntimeError(
                    "SQLite retirement mode is enabled; set POSTGRES_SENDER_ENABLED=true."
                )
            sender_stats = process_payout_events(session, dry_run=settings.dry_run)

        audit_event = build_payout_audit_event(
            session,
            attempt_id=attempt_id,
            attempted_at=attempt_time,
            period_start=settlement_result.period_start,
            period_end=settlement_result.period_end,
            snapshots_created=snapshots_created,
            settlement_id=settlement_result.settlement_id,
            settlement_status=settlement_result.status,
            reward_mode=reward_mode,
            pool_reward_btc=settlement_result.pool_reward_btc,
            total_work_btc_basis=settlement_result.total_work,
            total_share_delta=settlement_result.total_shares,
            block_reward=block_reward_payload,
            contribution_window_start=settlement_kwargs.get("work_window_start"),
            contribution_window_end=settlement_kwargs.get("work_window_end"),
            settlement_engine=settlement_engine,
        )
        try:
            write_payout_audit_log(settings.payout_audit_log_path, audit_event)
        except OSError:
            _write_scheduler_event(
                "audit_log_write_failed",
                {
                    "attempt_id": attempt_id,
                    "audit_log_path": settings.payout_audit_log_path,
                },
            )

        if settings.postgres_ledger_shadow_write_enabled and settlement_engine != "postgres":
            effective_work_window_start = settlement_kwargs.get("work_window_start") or settlement_result.period_start
            effective_work_window_end = settlement_kwargs.get("work_window_end") or settlement_result.period_end
            try:
                postgres_shadow_write = _shadow_write_postgres_settlement(
                    session,
                    settlement_id=settlement_result.settlement_id,
                    settlement_status=settlement_result.status,
                    settlement_period_start=settlement_result.period_start,
                    settlement_period_end=settlement_result.period_end,
                    settlement_pool_reward_btc=settlement_result.pool_reward_btc,
                    settlement_total_work=settlement_result.total_work,
                    settlement_total_shares=settlement_result.total_shares,
                    work_window_start=effective_work_window_start,
                    work_window_end=effective_work_window_end,
                    settings=settings,
                )
            except Exception as exc:
                postgres_shadow_write = {
                    "enabled": True,
                    "status": "failed",
                    "settlement_id": settlement_result.settlement_id,
                    "error": str(exc),
                }
                _write_scheduler_event(
                    "postgres_shadow_write_failed",
                    {
                        "settlement_id": settlement_result.settlement_id,
                        "error": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                )
        elif settlement_engine == "postgres":
            postgres_shadow_write = {
                "enabled": True,
                "status": "skipped_postgres_settlement_engine",
                "settlement_id": settlement_result.settlement_id,
                "note": "Postgres settlement already persisted to Postgres.",
            }

        if settings.enable_settlement_replay_hook:
            try:
                run_settlement_replay_hook(session, settlement_result)
            except Exception:
                pass

    response = {
        "snapshots_created": snapshots_created,
        "settlement_skipped": False,
        "settlement": {
            "settlement_id": settlement_result.settlement_id,
            "status": settlement_result.status,
            "period_start": settlement_result.period_start.isoformat(),
            "period_end": settlement_result.period_end.isoformat(),
            "user_count": settlement_result.user_count,
            "total_shares": settlement_result.total_shares,
            "total_work": _to_decimal_str(settlement_result.total_work),
            "pool_reward_btc": _to_decimal_str(settlement_result.pool_reward_btc),
            "carry_btc": _to_decimal_str(settlement_result.carry_btc),
        },
        "sender": {
            "attempted": sender_stats.attempted,
            "sent": sender_stats.sent,
            "failed": sender_stats.failed,
            "created_events": sender_stats.created_events,
        },
    }

    if block_reward_payload is not None:
        response["block_reward"] = block_reward_payload
    elif settings.translator_channels_url and reward_mode == "blocks":
        response["block_reward"] = {
            "reward_mode": "blocks",
            "block_reward_btc": _to_decimal_str(block_reward_btc),
            "interval_blocks": int(interval_blocks),
            "computed_reward_btc": _to_decimal_str(Decimal(interval_blocks) * block_reward_btc),
            "channels": block_delta_details,
        }
    if postgres_shadow_write is not None:
        response["postgres_shadow_write"] = postgres_shadow_write

    return response
