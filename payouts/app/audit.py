from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
import shutil

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.delta import compute_user_contribution_deltas
from app.mapping import parse_identity
from app.models import MetricSnapshot, User, UserPayout
from app.config import load_settings
from app.postgres_db import make_postgres_engine, make_postgres_session_factory
from app.postgres_delta import compute_user_contribution_deltas_postgres
from app.postgres_repositories import PostgresLedgerRepository
from app.runtime_cutover import should_fail_closed_on_postgres_primary

ZERO = Decimal("0")


def _as_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_decimal(value: object | None) -> Decimal:
    if value is None:
        return ZERO
    return Decimal(str(value))


def _to_decimal_str(value: object | None) -> str:
    return f"{_to_decimal(value):.8f}"


def _build_snapshot_alignment(
    session: Session,
    period_start: datetime,
    period_end: datetime,
) -> dict[str, object]:
    settings = load_settings()
    
    # Try Postgres path first if primary session enabled
    if getattr(settings, "postgres_primary_session_enabled", False):
        try:
            pg_period_start = _as_utc_aware(period_start)
            pg_period_end = _as_utc_aware(period_end)
            postgres_repo = PostgresLedgerRepository(
                make_postgres_session_factory(make_postgres_engine())
            )
            rows = postgres_repo.list_raw_miner_snapshot_counters_up_to(period_end=pg_period_end)
            if rows:
                return _build_snapshot_alignment_from_rows(rows, pg_period_start, pg_period_end)
        except Exception:
            if should_fail_closed_on_postgres_primary(
                postgres_primary_session_enabled=settings.postgres_primary_session_enabled,
                sqlite_retirement_mode_enabled=settings.sqlite_retirement_mode_enabled,
            ):
                raise RuntimeError(
                    "Postgres snapshot alignment read failed while Postgres primary is enabled."
                )
            pass  # Fall back to SQLite below
    
    # SQLite path
    rows = session.execute(
        select(MetricSnapshot)
        .where(MetricSnapshot.created_at <= period_end)
        .order_by(
            MetricSnapshot.identity.asc(),
            MetricSnapshot.channel_id.asc(),
            MetricSnapshot.created_at.asc(),
            MetricSnapshot.id.asc(),
        )
    ).scalars().all()

    return _build_snapshot_alignment_from_rows(rows, period_start, period_end)


def _build_snapshot_alignment_from_rows(
    rows: list,
    period_start: datetime,
    period_end: datetime,
) -> dict[str, object]:
    """Build snapshot alignment from row data (works with both SQLite ORM and Postgres dicts)."""
    grouped: dict[tuple[str, int], list] = defaultdict(list)
    for row in rows:
        # Handle both SQLite ORM objects and Postgres dicts
        identity = row.identity if hasattr(row, 'identity') else row['identity']
        channel_id = int(row.channel_id or 0) if hasattr(row, 'channel_id') else int(row.get('channel_id') or 0)
        grouped[(identity, channel_id)].append(row)

    per_miner: list[dict[str, object]] = []
    latest_per_miner: list[dict[str, object]] = []
    total_share_delta = 0
    total_work_delta = ZERO
    reset_count = 0
    total_snapshots_upto_end = len(rows)
    snapshots_in_window = 0
    miners_with_in_window_snapshot = 0
    miners_without_in_window_snapshot = 0

    for (identity, channel_id), samples in grouped.items():
        baseline = None
        current = None
        latest = samples[-1]

        # Extract row data - handle both ORM and dict objects
        def get_row_data(row):
            if hasattr(row, 'id'):  # SQLite ORM
                return {
                    'id': row.id,
                    'created_at': row.created_at,
                    'captured_at': row.created_at,  # SQLite MetricSnapshot doesn't have captured_at
                    'accepted_shares_total': row.accepted_shares_total,
                    'accepted_work_total': row.accepted_work_total,
                }
            else:  # Postgres dict
                return {
                    'id': row.get('id'),
                    'created_at': row.get('created_at') or row.get('captured_at'),
                    'captured_at': row.get('captured_at'),
                    'accepted_shares_total': row.get('accepted_shares_total'),
                    'accepted_work_total': row.get('accepted_work_total'),
                }
        
        latest_data = get_row_data(latest)
        latest_per_miner.append(
            {
                "identity": identity,
                "channel_id": channel_id,
                "latest_snapshot_id": latest_data['id'],
                "latest_at": latest_data['created_at'].isoformat() if hasattr(latest_data['created_at'], 'isoformat') else str(latest_data['created_at']),
                "latest_shares": int(latest_data['accepted_shares_total'] or 0),
                "latest_work": _to_decimal_str(latest_data['accepted_work_total']),
            }
        )

        for sample in samples:
            sample_data = get_row_data(sample)
            created_at = sample_data['created_at']
            if created_at < period_start:
                baseline = sample_data
                continue
            if created_at <= period_end:
                current = sample_data
                snapshots_in_window += 1

        if current is None:
            miners_without_in_window_snapshot += 1
            continue

        miners_with_in_window_snapshot += 1

        previous = baseline or current
        previous_shares = int(previous['accepted_shares_total'] or 0)
        current_shares = int(current['accepted_shares_total'] or 0)
        previous_work = _to_decimal(previous['accepted_work_total'])
        current_work = _to_decimal(current['accepted_work_total'])

        shares_reset = current_shares < previous_shares
        work_reset = current_work < previous_work

        share_delta = current_shares - previous_shares if not shares_reset else 0
        work_delta = current_work - previous_work if not work_reset else ZERO

        if shares_reset or work_reset:
            reset_count += 1

        total_share_delta += share_delta
        total_work_delta += work_delta

        per_miner.append(
            {
                "identity": identity,
                "channel_id": channel_id,
                "baseline_snapshot_id": previous['id'],
                "baseline_at": previous['created_at'].isoformat() if hasattr(previous['created_at'], 'isoformat') else str(previous['created_at']),
                "current_snapshot_id": current['id'],
                "current_at": current['created_at'].isoformat() if hasattr(current['created_at'], 'isoformat') else str(current['created_at']),
                "baseline_shares": previous_shares,
                "current_shares": current_shares,
                "share_delta": share_delta,
                "baseline_work": _to_decimal_str(previous_work),
                "current_work": _to_decimal_str(current_work),
                "work_delta": _to_decimal_str(work_delta),
                "reset_detected": shares_reset or work_reset,
            }
        )

    return {
        "miners": per_miner,
        "miner_count": len(per_miner),
        "total_share_delta": int(total_share_delta),
        "total_work_delta": _to_decimal_str(total_work_delta),
        "reset_count": int(reset_count),
        "coverage": {
            "total_snapshots_upto_period_end": int(total_snapshots_upto_end),
            "snapshots_in_window": int(snapshots_in_window),
            "snapshots_before_window": int(total_snapshots_upto_end - snapshots_in_window),
            "tracked_miners_total": int(len(grouped)),
            "miners_with_in_window_snapshot": int(miners_with_in_window_snapshot),
            "miners_without_in_window_snapshot": int(miners_without_in_window_snapshot),
        },
        "latest_snapshot_state": latest_per_miner,
    }


def _build_payout_rows(session: Session, settlement_id: int) -> list[dict[str, object]]:
    settings = load_settings()
    
    # Try Postgres path first if primary session enabled
    if getattr(settings, "postgres_primary_session_enabled", False):
        try:
            postgres_repo = PostgresLedgerRepository(
                make_postgres_session_factory(make_postgres_engine())
            )
            credit_rows = postgres_repo.list_settlement_user_credits_with_users(settlement_id)
            work_rows = postgres_repo.list_settlement_user_work_with_users(settlement_id)
            work_by_user: dict[str, dict[str, object]] = {
                row["username"]: row for row in work_rows
            }
            
            result = []
            for credit_row in credit_rows:
                username = credit_row["username"]
                work_row = work_by_user.get(username, {})
                
                # Convert sats back to BTC
                amount_sats = int(credit_row["amount_sats"] or 0)
                amount_btc = Decimal(str(amount_sats)) / Decimal("100000000")
                
                result.append(
                    {
                        "username": username,
                        "amount_btc": _to_decimal_str(amount_btc),
                        "status": credit_row["status"],
                        "payout_fraction": f"{_to_decimal(work_row.get('payout_fraction', 0)):.12f}",
                        "contribution_value": _to_decimal_str(work_row.get("share_delta", 0)),
                    }
                )
            
            return result
        except Exception:
            if should_fail_closed_on_postgres_primary(
                postgres_primary_session_enabled=settings.postgres_primary_session_enabled,
                sqlite_retirement_mode_enabled=settings.sqlite_retirement_mode_enabled,
            ):
                raise RuntimeError(
                    "Postgres payout rows read failed while Postgres primary is enabled."
                )
            pass  # Fall back to SQLite below
    
    # SQLite fallback
    rows = session.execute(
        select(
            User.username,
            UserPayout.amount_btc,
            UserPayout.status,
            UserPayout.payout_fraction,
            UserPayout.contribution_value,
        )
        .join(User, User.id == UserPayout.user_id)
        .where(UserPayout.settlement_id == settlement_id)
        .order_by(User.username.asc())
    ).all()
    return [
        {
            "username": username,
            "amount_btc": _to_decimal_str(amount_btc),
            "status": status,
            "payout_fraction": f"{_to_decimal(payout_fraction):.12f}",
            "contribution_value": _to_decimal_str(contribution_value),
        }
        for username, amount_btc, status, payout_fraction, contribution_value in rows
    ]


def _build_user_contributions(
    session: Session,
    period_start: datetime,
    period_end: datetime,
) -> list[dict[str, object]]:
    settings = load_settings()

    if getattr(settings, "postgres_primary_session_enabled", False):
        try:
            postgres_repo = PostgresLedgerRepository(
                make_postgres_session_factory(make_postgres_engine())
            )
            _start = _as_utc_aware(period_start)
            _end = _as_utc_aware(period_end)
            contributions = compute_user_contribution_deltas_postgres(postgres_repo, _start, _end)
            return [
                {
                    "username": username,
                    "share_delta": int(item.share_delta),
                    "work_delta": _to_decimal_str(item.work_delta),
                }
                for username, item in sorted(contributions.items(), key=lambda entry: entry[0])
            ]
        except Exception:
            if should_fail_closed_on_postgres_primary(
                postgres_primary_session_enabled=settings.postgres_primary_session_enabled,
                sqlite_retirement_mode_enabled=settings.sqlite_retirement_mode_enabled,
            ):
                raise RuntimeError(
                    "Postgres user contributions read failed while Postgres primary is enabled."
                )
            pass  # Fall back to SQLite below

    contributions = compute_user_contribution_deltas(session, period_start, period_end)
    return [
        {
            "username": username,
            "share_delta": int(item.share_delta),
            "work_delta": _to_decimal_str(item.work_delta),
        }
        for username, item in sorted(contributions.items(), key=lambda entry: entry[0])
    ]


def _find_unrewarded_users(
    user_contributions: list[dict[str, object]],
    payout_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    payout_by_username = {
        str(row["username"]): _to_decimal(row.get("amount_btc"))
        for row in payout_rows
    }

    unrewarded: list[dict[str, object]] = []
    for contribution in user_contributions:
        username = str(contribution["username"])
        work_delta = _to_decimal(contribution.get("work_delta"))
        share_delta = int(contribution.get("share_delta") or 0)
        amount = payout_by_username.get(username, ZERO)
        if (work_delta > ZERO or share_delta > 0) and amount <= ZERO:
            unrewarded.append(
                {
                    "username": username,
                    "share_delta": share_delta,
                    "work_delta": _to_decimal_str(work_delta),
                    "payout_amount_btc": _to_decimal_str(amount),
                }
            )

    return unrewarded


def build_payout_audit_event(
    session: Session,
    *,
    attempt_id: str,
    attempted_at: datetime,
    period_start: datetime,
    period_end: datetime,
    snapshots_created: int,
    settlement_id: int,
    settlement_status: str,
    reward_mode: str,
    pool_reward_btc: Decimal,
    total_work_btc_basis: Decimal,
    total_share_delta: int,
    block_reward: dict[str, object] | None = None,
    contribution_window_start: datetime | None = None,
    contribution_window_end: datetime | None = None,
    settlement_engine: str = "sqlite",
) -> dict[str, object]:
    effective_contribution_window_start = contribution_window_start or period_start
    effective_contribution_window_end = contribution_window_end or period_end

    snapshot_alignment = _build_snapshot_alignment(
        session,
        effective_contribution_window_start,
        effective_contribution_window_end,
    )
    payout_rows = _build_payout_rows(session, settlement_id)
    user_contributions = _build_user_contributions(
        session,
        effective_contribution_window_start,
        effective_contribution_window_end,
    )
    unrewarded_users = _find_unrewarded_users(user_contributions, payout_rows)

    identities_without_username = 0
    for miner_row in snapshot_alignment["miners"]:
        identity = str(miner_row["identity"])
        try:
            parse_identity(identity)
        except ValueError:
            identities_without_username += 1

    event: dict[str, object] = {
        "attempt_id": attempt_id,
        "attempted_at": attempted_at.isoformat(),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "contribution_window_start": effective_contribution_window_start.isoformat(),
        "contribution_window_end": effective_contribution_window_end.isoformat(),
        "snapshots_created": int(snapshots_created),
        "settlement": {
            "settlement_id": int(settlement_id),
            "status": settlement_status,
            "reward_mode": reward_mode,
            "pool_reward_btc": _to_decimal_str(pool_reward_btc),
            "total_work": _to_decimal_str(total_work_btc_basis),
            "total_shares": int(total_share_delta),
            "engine": settlement_engine,
        },
        "snapshot_alignment": snapshot_alignment,
        "user_contributions": user_contributions,
        "payout_rows": payout_rows,
        "checks": {
            "identities_without_username": int(identities_without_username),
            "unrewarded_user_count": len(unrewarded_users),
            "unrewarded_users": unrewarded_users,
        },
    }
    if block_reward is not None:
        event["block_reward"] = block_reward

    return event


def write_payout_audit_log(log_path: str, event: dict[str, object]) -> None:
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(event, sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(payload)
        handle.write("\n")

def rotate_payout_audit_log(path: str) -> str | None:
    log_path = Path(path)
    if not log_path.exists() or log_path.stat().st_size == 0:
        return None

    archive_dir = log_path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_name = f"{log_path.stem}.{timestamp}{log_path.suffix}"
    archive_path = archive_dir / archive_name
    shutil.move(str(log_path), str(archive_path))
    return str(archive_path)


def read_recent_audit_entries(log_path: str, limit: int = 50) -> dict[str, object]:
    normalized_limit = max(1, min(int(limit), 500))
    path = Path(log_path)
    if not path.exists():
        return {
            "log_path": str(path),
            "exists": False,
            "entry_count": 0,
            "entries": [],
        }

    lines = path.read_text(encoding="utf-8").splitlines()
    parsed_entries: list[dict[str, object]] = []
    invalid_line_count = 0
    for line in lines[-normalized_limit:]:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            invalid_line_count += 1
            continue
        if isinstance(payload, dict):
            parsed_entries.append(payload)

    return {
        "log_path": str(path),
        "exists": True,
        "entry_count": len(parsed_entries),
        "invalid_line_count": int(invalid_line_count),
        "entries": parsed_entries,
    }