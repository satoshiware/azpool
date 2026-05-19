from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

from payouts.collector.app.delta import SnapshotCounters, compute_delta, is_counter_reset


def _snapshot(
    shares: str,
    work: str,
    *,
    seq: int | None = 10,
    at: datetime | None = None,
) -> SnapshotCounters:
    return SnapshotCounters(
        shares_accepted=Decimal(shares),
        share_work_sum=Decimal(work),
        last_share_sequence_number=seq,
        observed_at=at or datetime(2026, 5, 19, 12, 0, tzinfo=UTC),
    )


def test_compute_delta_increased_counters() -> None:
    previous = _snapshot("10", "100.0", seq=5, at=datetime(2026, 5, 19, 12, 0, tzinfo=UTC))
    current = _snapshot("15", "150.5", seq=9, at=datetime(2026, 5, 19, 12, 5, tzinfo=UTC))

    delta = compute_delta(
        pool_instance_id="pool01",
        client_id=1,
        channel_type="extended",
        channel_id=42,
        previous=previous,
        current=current,
    )

    assert delta is not None
    assert delta.accepted_delta == Decimal("5")
    assert delta.work_delta == Decimal("50.5")
    assert delta.from_sequence_number == 5
    assert delta.to_sequence_number == 9
    assert delta.reset_detected is False
    assert "pool01:1:extended:42:5:9:" in delta.idempotency_key


def test_compute_delta_equal_counters_returns_none() -> None:
    previous = _snapshot("10", "100.0")
    current = _snapshot("10", "100.0", at=datetime(2026, 5, 19, 12, 5, tzinfo=UTC))

    assert compute_delta(
        pool_instance_id="pool01",
        client_id=1,
        channel_type="extended",
        channel_id=42,
        previous=previous,
        current=current,
    ) is None


def test_compute_delta_decreased_counters_treated_as_reset() -> None:
    previous = _snapshot("20", "200.0")
    current = _snapshot("5", "50.0", at=datetime(2026, 5, 19, 12, 5, tzinfo=UTC))

    assert is_counter_reset(previous, current) is True
    assert compute_delta(
        pool_instance_id="pool01",
        client_id=1,
        channel_type="standard",
        channel_id=7,
        previous=previous,
        current=current,
    ) is None


@pytest.mark.parametrize(
    ("previous_work", "current_work", "expected_work_delta"),
    [
        ("100.0", "125.0", "25.0"),
        ("0", "93130.0", "93130.0"),
    ],
)
def test_work_delta_formula(previous_work: str, current_work: str, expected_work_delta: str) -> None:
    previous = _snapshot("1", previous_work)
    current = _snapshot("2", current_work, at=datetime(2026, 5, 19, 12, 1, tzinfo=UTC))

    delta = compute_delta(
        pool_instance_id="pool02",
        client_id=2,
        channel_type="extended",
        channel_id=3,
        previous=previous,
        current=current,
    )

    assert delta is not None
    assert delta.work_delta == Decimal(expected_work_delta)
