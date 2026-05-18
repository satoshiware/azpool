from __future__ import annotations

import pytest

from app.config import load_settings
from app.main import _new_session


PRIMARY_SESSION_ENV_KEYS = [
    "POSTGRES_PRIMARY_SESSION_ENABLED",
    "POSTGRES_PRIMARY_SESSION_FALLBACK_TO_SQLITE",
    "SQLITE_RETIREMENT_MODE_ENABLED",
    "SQLITE_RUNTIME_WRITES_ENABLED",
]


def _clear_primary_session_env(monkeypatch) -> None:
    for key in PRIMARY_SESSION_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_primary_session_defaults_are_safe(monkeypatch) -> None:
    _clear_primary_session_env(monkeypatch)

    settings = load_settings()

    assert settings.postgres_primary_session_enabled is False
    assert settings.postgres_primary_session_fallback_to_sqlite is True
    assert settings.sqlite_retirement_mode_enabled is False
    assert settings.sqlite_runtime_writes_enabled is True


def test_primary_session_flags_parse_from_env(monkeypatch) -> None:
    _clear_primary_session_env(monkeypatch)
    monkeypatch.setenv("POSTGRES_PRIMARY_SESSION_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_PRIMARY_SESSION_FALLBACK_TO_SQLITE", "false")

    settings = load_settings()

    assert settings.postgres_primary_session_enabled is True
    assert settings.postgres_primary_session_fallback_to_sqlite is False


def test_sqlite_retirement_flags_parse_from_env(monkeypatch) -> None:
    _clear_primary_session_env(monkeypatch)
    monkeypatch.setenv("SQLITE_RETIREMENT_MODE_ENABLED", "true")
    monkeypatch.setenv("SQLITE_RUNTIME_WRITES_ENABLED", "false")

    settings = load_settings()

    assert settings.sqlite_retirement_mode_enabled is True
    assert settings.sqlite_runtime_writes_enabled is False


def test_new_session_falls_back_to_sqlite_when_postgres_primary_fails(monkeypatch) -> None:
    _clear_primary_session_env(monkeypatch)
    monkeypatch.setenv("POSTGRES_PRIMARY_SESSION_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_PRIMARY_SESSION_FALLBACK_TO_SQLITE", "true")

    monkeypatch.setattr(
        "app.main.make_postgres_engine",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("pg down")),
    )

    session = _new_session()
    try:
        assert session is not None
    finally:
        session.close()


def test_new_session_raises_when_postgres_primary_fails_and_fallback_disabled(monkeypatch) -> None:
    _clear_primary_session_env(monkeypatch)
    monkeypatch.setenv("POSTGRES_PRIMARY_SESSION_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_PRIMARY_SESSION_FALLBACK_TO_SQLITE", "false")

    monkeypatch.setattr(
        "app.main.make_postgres_engine",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("pg down")),
    )

    with pytest.raises(RuntimeError, match="pg down"):
        _new_session()


def test_retirement_preflight_reports_missing_required_flags(monkeypatch) -> None:
    from app.main import _retirement_preflight_errors

    _clear_primary_session_env(monkeypatch)
    monkeypatch.setenv("SQLITE_RETIREMENT_MODE_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_SETTLEMENT_ENGINE_ENABLED", "false")
    monkeypatch.setenv("POSTGRES_SENDER_ENABLED", "false")

    settings = load_settings()
    errors = _retirement_preflight_errors(settings)

    assert len(errors) > 0
    assert any("POSTGRES_PRIMARY_SESSION_ENABLED" in item for item in errors)
    assert any("POSTGRES_SETTLEMENT_ENGINE_ENABLED" in item for item in errors)
    assert any("POSTGRES_SENDER_ENABLED" in item for item in errors)


def test_retirement_preflight_rejects_block_event_rewards(monkeypatch) -> None:
    from app.main import _retirement_preflight_errors

    _clear_primary_session_env(monkeypatch)
    monkeypatch.setenv("SQLITE_RETIREMENT_MODE_ENABLED", "true")
    monkeypatch.setenv("ENABLE_BLOCK_EVENT_REWARDS", "true")

    settings = load_settings()
    errors = _retirement_preflight_errors(settings)

    assert any("ENABLE_BLOCK_EVENT_REWARDS" in item for item in errors)
