"""
Unit tests for Phase 6 shadow compare migration logic.

All tests are pure-logic and run without a real Postgres connection or FastAPI.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.postgres_shadow_compare import (
    _postgres_primary_compare,
    _postgres_primary_audit,
    PostgresShadowCompareError,
    _to_decimal_str,
    _normalize_work,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settlement_window(
    *,
    settlement_id: int = 1,
    status: str = "complete",
    total_reward_sats: int = 5_000_000,
    total_work: str = "1.00000000",
    total_shares: int = 200,
    user_payouts: list[dict] | None = None,
    blocks: list[dict] | None = None,
) -> dict:
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    return {
        "id": settlement_id,
        "status": status,
        "settlement_run_at": now,
        "work_window_start": now,
        "work_window_end": now,
        "total_reward_sats": total_reward_sats,
        "total_work": Decimal(total_work),
        "total_shares": total_shares,
    }


def _make_repo(
    *,
    window: dict | None = None,
    credits: list[dict] | None = None,
    blocks: list[dict] | None = None,
    windows: list[dict] | None = None,
    raise_on_get: Exception | None = None,
    raise_on_list: Exception | None = None,
) -> MagicMock:
    repo = MagicMock()
    if raise_on_get:
        repo.get_settlement_window_by_id.side_effect = raise_on_get
    else:
        repo.get_settlement_window_by_id.return_value = window
    repo.list_settlement_user_credits_with_users.return_value = credits or []
    repo.list_settlement_blocks.return_value = blocks or []
    if raise_on_list:
        repo.list_settlement_windows_paginated.side_effect = raise_on_list
    else:
        repo.list_settlement_windows_paginated.return_value = windows or []
    return repo


CHECKED_AT = "2025-01-01T12:00:00+00:00"


# ---------------------------------------------------------------------------
# _postgres_primary_compare — single settlement
# ---------------------------------------------------------------------------

def test_primary_compare_returns_postgres_primary_status_when_found() -> None:
    window = _make_settlement_window(settlement_id=42)
    repo = _make_repo(window=window)
    payload, code = _postgres_primary_compare(repo, 42, checked_at=CHECKED_AT)
    assert code == 200
    assert payload["comparison_status"] == "postgres_primary"
    assert payload["sqlite_summary"] is None
    assert payload["postgres_summary"] is not None
    assert payload["mismatches"] == []


def test_primary_compare_returns_not_found_when_window_missing() -> None:
    repo = _make_repo(window=None)
    payload, code = _postgres_primary_compare(repo, 99, checked_at=CHECKED_AT)
    assert code == 404
    assert payload["comparison_status"] == "not_found"
    assert payload["sqlite_summary"] is None
    assert payload["postgres_summary"] is None


def test_primary_compare_returns_error_on_postgres_failure() -> None:
    repo = _make_repo(raise_on_get=RuntimeError("db down"))
    payload, code = _postgres_primary_compare(repo, 1, checked_at=CHECKED_AT)
    assert code == 503
    assert payload["comparison_status"] == "error"
    assert "db down" in str(payload["error"])


def test_primary_compare_postgres_summary_shape() -> None:
    window = _make_settlement_window(
        settlement_id=5,
        total_reward_sats=100_000_000,
        total_work="2.50000000",
        total_shares=1000,
    )
    credits = [
        {"username": "alice", "amount_sats": 60_000_000, "status": "sent", "idempotency_key": "k1"},
        {"username": "bob", "amount_sats": 40_000_000, "status": "sent", "idempotency_key": "k2"},
    ]
    blocks = [
        {"reward_sats": 100_000_000},
    ]
    repo = _make_repo(window=window, credits=credits, blocks=blocks)
    payload, code = _postgres_primary_compare(repo, 5, checked_at=CHECKED_AT)
    summary = payload["postgres_summary"]
    assert code == 200
    assert summary["settlement_window_id"] == 5
    assert summary["total_reward_sats"] == 100_000_000
    assert summary["user_payout_count"] == 2
    assert summary["rewarded_block_count"] == 1
    assert summary["rewarded_block_total_sats"] == 100_000_000


# ---------------------------------------------------------------------------
# _postgres_primary_audit — bulk audit
# ---------------------------------------------------------------------------

def test_primary_audit_returns_postgres_primary_rows() -> None:
    windows = [
        _make_settlement_window(settlement_id=1),
        _make_settlement_window(settlement_id=2),
    ]
    repo = _make_repo(windows=windows)
    payload, code = _postgres_primary_audit(repo, limit=10, offset=0, checked_at=CHECKED_AT)
    assert code == 200
    assert payload["comparison_status"] == "postgres_primary"
    assert payload["total_checked"] == 2
    assert len(payload["rows"]) == 2
    for row in payload["rows"]:
        assert row["comparison_status"] == "postgres_primary"
        assert row["mismatch_count"] == 0


def test_primary_audit_empty_returns_ok() -> None:
    repo = _make_repo(windows=[])
    payload, code = _postgres_primary_audit(repo, limit=10, offset=0, checked_at=CHECKED_AT)
    assert code == 200
    assert payload["total_checked"] == 0
    assert payload["rows"] == []


def test_primary_audit_status_filter_non_postgres_primary_returns_empty_rows() -> None:
    windows = [_make_settlement_window(settlement_id=1)]
    repo = _make_repo(windows=windows)
    payload, code = _postgres_primary_audit(
        repo, limit=10, offset=0, status_filter="matched", checked_at=CHECKED_AT
    )
    # "postgres_primary" rows are filtered out when status_filter="matched"
    assert code == 200
    assert payload["rows"] == []
    assert payload["total_checked"] == 1  # still counted, just filtered from display


def test_primary_audit_status_filter_postgres_primary_includes_rows() -> None:
    windows = [_make_settlement_window(settlement_id=1)]
    repo = _make_repo(windows=windows)
    payload, code = _postgres_primary_audit(
        repo, limit=10, offset=0, status_filter="postgres_primary", checked_at=CHECKED_AT
    )
    assert code == 200
    assert len(payload["rows"]) == 1


def test_primary_audit_returns_error_on_postgres_failure() -> None:
    repo = _make_repo(raise_on_list=RuntimeError("query failed"))
    payload, code = _postgres_primary_audit(repo, limit=10, offset=0, checked_at=CHECKED_AT)
    assert code == 503
    assert payload["comparison_status"] == "error"
    assert "query failed" in str(payload["error"])


def test_primary_audit_include_details_adds_mismatches_key() -> None:
    windows = [_make_settlement_window(settlement_id=1)]
    repo = _make_repo(windows=windows)
    payload, code = _postgres_primary_audit(
        repo, limit=10, offset=0, include_details=True, checked_at=CHECKED_AT
    )
    assert code == 200
    for row in payload["rows"]:
        assert "mismatches" in row
        assert row["mismatches"] == []
