from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class SnapshotCounters:
    shares_accepted: Decimal
    share_work_sum: Decimal
    last_share_sequence_number: int | None
    observed_at: datetime


@dataclass(frozen=True)
class DeltaComputation:
    accepted_delta: Decimal
    work_delta: Decimal
    from_sequence_number: int | None
    to_sequence_number: int | None
    observed_from: datetime
    observed_to: datetime
    reset_detected: bool
    idempotency_key: str


def _to_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def build_idempotency_key(
    *,
    pool_instance_id: str,
    client_id: int,
    channel_type: str,
    channel_id: int,
    from_sequence_number: int | None,
    to_sequence_number: int | None,
    observed_to: datetime,
) -> str:
    observed_bucket = observed_to.astimezone().strftime("%Y%m%dT%H%M%S")
    from_seq = "" if from_sequence_number is None else str(from_sequence_number)
    to_seq = "" if to_sequence_number is None else str(to_sequence_number)
    return (
        f"{pool_instance_id}:{client_id}:{channel_type}:{channel_id}:"
        f"{from_seq}:{to_seq}:{observed_bucket}"
    )


def compute_delta(
    *,
    pool_instance_id: str,
    client_id: int,
    channel_type: str,
    channel_id: int,
    previous: SnapshotCounters,
    current: SnapshotCounters,
) -> DeltaComputation | None:
    """Return a positive delta when counters increased; None when unchanged or reset."""
    prev_shares = _to_decimal(previous.shares_accepted)
    prev_work = _to_decimal(previous.share_work_sum)
    curr_shares = _to_decimal(current.shares_accepted)
    curr_work = _to_decimal(current.share_work_sum)

    shares_increased = curr_shares > prev_shares
    work_increased = curr_work > prev_work
    shares_equal = curr_shares == prev_shares
    work_equal = curr_work == prev_work

    if shares_equal and work_equal:
        return None

    reset_detected = curr_shares < prev_shares or curr_work < prev_work
    if reset_detected:
        return None

    if not shares_increased and not work_increased:
        return None

    accepted_delta = curr_shares - prev_shares if shares_increased else Decimal("0")
    work_delta = curr_work - prev_work if work_increased else Decimal("0")

    return DeltaComputation(
        accepted_delta=accepted_delta,
        work_delta=work_delta,
        from_sequence_number=previous.last_share_sequence_number,
        to_sequence_number=current.last_share_sequence_number,
        observed_from=previous.observed_at,
        observed_to=current.observed_at,
        reset_detected=False,
        idempotency_key=build_idempotency_key(
            pool_instance_id=pool_instance_id,
            client_id=client_id,
            channel_type=channel_type,
            channel_id=channel_id,
            from_sequence_number=previous.last_share_sequence_number,
            to_sequence_number=current.last_share_sequence_number,
            observed_to=current.observed_at,
        ),
    )


def is_counter_reset(previous: SnapshotCounters, current: SnapshotCounters) -> bool:
    prev_shares = _to_decimal(previous.shares_accepted)
    prev_work = _to_decimal(previous.share_work_sum)
    curr_shares = _to_decimal(current.shares_accepted)
    curr_work = _to_decimal(current.share_work_sum)
    return curr_shares < prev_shares or curr_work < prev_work
