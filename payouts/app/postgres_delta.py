from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.delta import (
    IdentityDelta,
    UserContribution,
    compute_counter_delta,
    compute_decimal_counter_delta,
)
from app.mapping import parse_identity
from app.postgres_repositories import PostgresLedgerRepository


ZERO = Decimal("0")


@dataclass(frozen=True)
class RawSnapshotCounter:
    identity: str
    channel_id: int | None
    accepted_shares_total: int
    accepted_work_total: Decimal
    captured_at: datetime


def _as_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_decimal(value: object | None) -> Decimal:
    if value is None:
        return ZERO
    return Decimal(str(value))


def compute_identity_share_deltas_postgres(
    repository: PostgresLedgerRepository,
    period_start: datetime,
    period_end: datetime,
) -> dict[str, IdentityDelta]:
    period_start_aware = _as_utc_aware(period_start)
    period_end_aware = _as_utc_aware(period_end)

    rows = repository.list_raw_miner_snapshot_counters_up_to(period_end=period_end_aware)

    grouped: dict[tuple[str, int | None], list[RawSnapshotCounter]] = defaultdict(list)
    for row in rows:
        item = RawSnapshotCounter(
            identity=str(row["identity"]),
            channel_id=int(row["channel_id"]) if row.get("channel_id") is not None else None,
            accepted_shares_total=int(row.get("accepted_shares_total") or 0),
            accepted_work_total=_to_decimal(row.get("accepted_work_total")),
            captured_at=_as_utc_aware(row["captured_at"]),
        )
        grouped[(item.identity, item.channel_id)].append(item)

    result: dict[str, IdentityDelta] = {}
    for (identity, _channel_id), samples in grouped.items():
        share_baseline: int | None = None
        work_baseline: Decimal | None = None
        in_window_shares: list[int] = []
        in_window_work: list[Decimal] = []

        for sample in samples:
            if sample.captured_at < period_start_aware:
                share_baseline = sample.accepted_shares_total
                work_baseline = sample.accepted_work_total
                continue
            in_window_shares.append(sample.accepted_shares_total)
            in_window_work.append(sample.accepted_work_total)

        share_series = ([share_baseline] if share_baseline is not None else []) + in_window_shares
        work_series = ([work_baseline] if work_baseline is not None else []) + in_window_work

        share_delta, share_resets = compute_counter_delta(share_series)
        work_delta, work_resets = compute_decimal_counter_delta(work_series)

        if share_delta <= 0 and work_delta <= ZERO:
            continue

        existing = result.get(identity)
        reset_count = max(share_resets, work_resets)
        sample_count = max(len(share_series), len(work_series))

        if existing is None:
            result[identity] = IdentityDelta(
                identity=identity,
                share_delta=share_delta,
                reset_count=reset_count,
                sample_count=sample_count,
                work_delta=work_delta,
                channel_count=1,
            )
            continue

        result[identity] = IdentityDelta(
            identity=identity,
            share_delta=existing.share_delta + share_delta,
            reset_count=existing.reset_count + reset_count,
            sample_count=existing.sample_count + sample_count,
            work_delta=existing.work_delta + work_delta,
            channel_count=existing.channel_count + 1,
        )

    return result


def compute_user_contribution_deltas_postgres(
    repository: PostgresLedgerRepository,
    period_start: datetime,
    period_end: datetime,
) -> dict[str, UserContribution]:
    identity_deltas = compute_identity_share_deltas_postgres(repository, period_start, period_end)

    user_shares: dict[str, int] = defaultdict(int)
    user_work: dict[str, Decimal] = defaultdict(lambda: ZERO)

    for identity, delta in identity_deltas.items():
        try:
            parts = parse_identity(identity)
        except ValueError:
            continue
        user_shares[parts.username] += int(delta.share_delta)
        user_work[parts.username] += Decimal(str(delta.work_delta))

    usernames = sorted(set(user_shares) | set(user_work))
    return {
        username: UserContribution(
            username=username,
            share_delta=int(user_shares.get(username, 0) or 0),
            work_delta=Decimal(str(user_work.get(username, ZERO))),
        )
        for username in usernames
    }
