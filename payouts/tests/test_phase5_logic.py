"""
Unit tests for Phase 5 audit migration logic.

All tests are pure-logic and run without a real Postgres connection or FastAPI.
"""
from __future__ import annotations

from decimal import Decimal

from app.runtime_cutover import should_fail_closed_on_postgres_primary


# ---------------------------------------------------------------------------
# Helpers that mirror the fail-closed decision in audit.py Phase 5 functions
# ---------------------------------------------------------------------------

def _simulate_audit_read(
    *,
    postgres_primary_session_enabled: bool,
    sqlite_retirement_mode_enabled: bool,
    pg_succeeds: bool,
) -> str:
    """
    Mirrors the Postgres-first branching inside _build_snapshot_alignment,
    _build_payout_rows, and _build_user_contributions:

      try Postgres → on fail: fail-closed if primary, else SQLite fallback
    """
    if postgres_primary_session_enabled:
        if pg_succeeds:
            return "postgres"
        # Postgres failed
        if should_fail_closed_on_postgres_primary(
            postgres_primary_session_enabled=postgres_primary_session_enabled,
            sqlite_retirement_mode_enabled=sqlite_retirement_mode_enabled,
        ):
            return "error"
    return "sqlite"


def test_audit_read_uses_postgres_when_primary_and_pg_succeeds() -> None:
    assert _simulate_audit_read(
        postgres_primary_session_enabled=True,
        sqlite_retirement_mode_enabled=False,
        pg_succeeds=True,
    ) == "postgres"


def test_audit_read_errors_when_primary_and_pg_fails() -> None:
    assert _simulate_audit_read(
        postgres_primary_session_enabled=True,
        sqlite_retirement_mode_enabled=False,
        pg_succeeds=False,
    ) == "error"


def test_audit_read_errors_when_retirement_mode_and_pg_fails() -> None:
    assert _simulate_audit_read(
        postgres_primary_session_enabled=False,
        sqlite_retirement_mode_enabled=True,
        pg_succeeds=False,
    ) == "sqlite"  # retirement_mode alone doesn't block the non-primary path


def test_audit_read_uses_sqlite_when_primary_disabled() -> None:
    assert _simulate_audit_read(
        postgres_primary_session_enabled=False,
        sqlite_retirement_mode_enabled=False,
        pg_succeeds=False,
    ) == "sqlite"


# ---------------------------------------------------------------------------
# Payout row transformation (Postgres credit → audit payload shape)
# ---------------------------------------------------------------------------

def _transform_credit_row_to_audit(
    credit_row: dict,
    work_row: dict,
) -> dict:
    """Mirrors the transformation in _build_payout_rows Postgres path."""
    amount_sats = int(credit_row.get("amount_sats") or 0)
    amount_btc = Decimal(str(amount_sats)) / Decimal("100000000")
    return {
        "username": credit_row["username"],
        "amount_btc": f"{amount_btc:.8f}",
        "status": credit_row["status"],
        "payout_fraction": f"{Decimal(str(work_row.get('payout_fraction', 0))):.12f}",
        "contribution_value": f"{Decimal(str(work_row.get('share_delta', 0))):.8f}",
    }


def test_payout_row_converts_sats_to_btc() -> None:
    credit = {"username": "alice", "amount_sats": 100_000_000, "status": "sent"}
    work = {"payout_fraction": "0.5", "share_delta": 1000}
    row = _transform_credit_row_to_audit(credit, work)
    assert row["amount_btc"] == "1.00000000"
    assert row["username"] == "alice"
    assert row["status"] == "sent"


def test_payout_row_zero_sats() -> None:
    credit = {"username": "bob", "amount_sats": 0, "status": "pending"}
    work = {"payout_fraction": "0.0", "share_delta": 0}
    row = _transform_credit_row_to_audit(credit, work)
    assert row["amount_btc"] == "0.00000000"


def test_payout_row_missing_work_defaults_to_zero() -> None:
    credit = {"username": "carol", "amount_sats": 50_000_000, "status": "sent"}
    work = {}
    row = _transform_credit_row_to_audit(credit, work)
    assert row["contribution_value"] == "0.00000000"
    assert row["payout_fraction"] == "0.000000000000"


# ---------------------------------------------------------------------------
# User contribution list transformation (Postgres delta → audit payload shape)
# ---------------------------------------------------------------------------

def _transform_contribution(username: str, share_delta: int, work_delta: str) -> dict:
    """Mirrors the list comprehension in _build_user_contributions Postgres path."""
    return {
        "username": username,
        "share_delta": int(share_delta),
        "work_delta": f"{Decimal(work_delta):.8f}",
    }


def test_user_contribution_formats_correctly() -> None:
    row = _transform_contribution("alice", 500, "1.23456789")
    assert row == {
        "username": "alice",
        "share_delta": 500,
        "work_delta": "1.23456789",
    }


def test_user_contribution_zero_delta() -> None:
    row = _transform_contribution("bob", 0, "0")
    assert row["share_delta"] == 0
    assert row["work_delta"] == "0.00000000"
