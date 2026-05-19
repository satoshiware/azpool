from __future__ import annotations


def should_fail_closed_on_postgres_primary(*, postgres_primary_session_enabled: bool, sqlite_retirement_mode_enabled: bool) -> bool:
    return bool(postgres_primary_session_enabled or sqlite_retirement_mode_enabled)