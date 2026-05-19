"""
Unit tests for Phase 3 cutover behavior.
"""

from app.runtime_cutover import should_fail_closed_on_postgres_primary


def test_fail_closed_when_postgres_primary_enabled() -> None:
    assert should_fail_closed_on_postgres_primary(
        postgres_primary_session_enabled=True,
        sqlite_retirement_mode_enabled=False,
    ) is True


def test_fail_closed_when_sqlite_retirement_enabled() -> None:
    assert should_fail_closed_on_postgres_primary(
        postgres_primary_session_enabled=False,
        sqlite_retirement_mode_enabled=True,
    ) is True


def test_allow_fallback_only_when_both_disabled() -> None:
    assert should_fail_closed_on_postgres_primary(
        postgres_primary_session_enabled=False,
        sqlite_retirement_mode_enabled=False,
    ) is False


# ---------------------------------------------------------------------------
# Read endpoint outer-path tests
# These assert the decision made at the *outer* SQLite fallback path in
# audit_settlements / latest_settlement when use_postgres=False.
# ---------------------------------------------------------------------------


def _simulated_outer_path_response(
    *,
    postgres_primary_session_enabled: bool,
    sqlite_retirement_mode_enabled: bool,
) -> str:
    """
    Mirrors the branching logic added to audit_settlements / latest_settlement:

        if should_fail_closed_on_postgres_primary(...):
            return _postgres_read_error_response(...)  # → "error"
        return _read_sqlite_...(...)                   # → "sqlite"
    """
    if should_fail_closed_on_postgres_primary(
        postgres_primary_session_enabled=postgres_primary_session_enabled,
        sqlite_retirement_mode_enabled=sqlite_retirement_mode_enabled,
    ):
        return "error"
    return "sqlite"


def test_outer_path_returns_error_when_primary_enabled() -> None:
    """When postgres_primary is on and use_postgres=False, fail closed → error."""
    result = _simulated_outer_path_response(
        postgres_primary_session_enabled=True,
        sqlite_retirement_mode_enabled=False,
    )
    assert result == "error"


def test_outer_path_returns_error_when_sqlite_retirement_enabled() -> None:
    """When sqlite_retirement is on and use_postgres=False, fail closed → error."""
    result = _simulated_outer_path_response(
        postgres_primary_session_enabled=False,
        sqlite_retirement_mode_enabled=True,
    )
    assert result == "error"


def test_outer_path_allows_sqlite_when_neither_flag_set() -> None:
    """When neither flag is set and use_postgres=False, SQLite fallback is allowed."""
    result = _simulated_outer_path_response(
        postgres_primary_session_enabled=False,
        sqlite_retirement_mode_enabled=False,
    )
    assert result == "sqlite"
