import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.main import app


POSTGRES_READ_ENV_KEYS = [
    "POSTGRES_LEDGER_READS_ENABLED",
    "POSTGRES_LEDGER_READ_FALLBACK_TO_SQLITE",
    "POSTGRES_LEDGER_READ_REQUIRE_SHADOW_MATCH",
    "POSTGRES_LEDGER_READ_ALLOWED_ENDPOINTS",
    "POSTGRES_LEDGER_READ_MODE",
]


def _clear_postgres_read_env(monkeypatch) -> None:
    for key in POSTGRES_READ_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_postgres_read_defaults_are_safe_sqlite(monkeypatch) -> None:
    _clear_postgres_read_env(monkeypatch)

    settings = load_settings()

    assert settings.postgres_ledger_reads_enabled is False
    assert settings.postgres_ledger_read_fallback_to_sqlite is True
    assert settings.postgres_ledger_read_require_shadow_match is True
    assert settings.postgres_ledger_read_allowed_endpoints == ()
    assert settings.postgres_ledger_read_mode == "sqlite"
    assert settings.effective_postgres_read_mode == "sqlite"


def test_postgres_reads_disabled_forces_effective_sqlite(monkeypatch) -> None:
    _clear_postgres_read_env(monkeypatch)
    monkeypatch.setenv("POSTGRES_LEDGER_READS_ENABLED", "false")
    monkeypatch.setenv("POSTGRES_LEDGER_READ_MODE", "postgres_authoritative")

    settings = load_settings()

    assert settings.postgres_ledger_read_mode == "postgres_authoritative"
    assert settings.effective_postgres_read_mode == "sqlite"


def test_postgres_read_allowed_endpoints_parses_trimmed_list(monkeypatch) -> None:
    _clear_postgres_read_env(monkeypatch)
    monkeypatch.setenv(
        "POSTGRES_LEDGER_READ_ALLOWED_ENDPOINTS",
        " settlement_history, ,settlement_detail, credits.report , ",
    )

    settings = load_settings()

    assert settings.postgres_ledger_read_allowed_endpoints == (
        "settlement_history",
        "settlement_detail",
        "credits.report",
    )
    assert settings.postgres_read_allowed_endpoints == {
        "settlement_history",
        "settlement_detail",
        "credits.report",
    }


def test_invalid_postgres_read_mode_fails_clearly(monkeypatch) -> None:
    _clear_postgres_read_env(monkeypatch)
    monkeypatch.setenv("POSTGRES_LEDGER_READ_MODE", "postgres_primary")

    with pytest.raises(ValueError, match="Invalid POSTGRES_LEDGER_READ_MODE"):
        load_settings()


def test_authoritative_mode_without_fallback_can_load_but_is_not_default(monkeypatch) -> None:
    _clear_postgres_read_env(monkeypatch)
    monkeypatch.setenv("POSTGRES_LEDGER_READS_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_LEDGER_READ_MODE", "postgres_authoritative")
    monkeypatch.setenv("POSTGRES_LEDGER_READ_FALLBACK_TO_SQLITE", "false")

    settings = load_settings()

    assert settings.postgres_ledger_read_mode == "postgres_authoritative"
    assert settings.effective_postgres_read_mode == "postgres_authoritative"
    assert settings.postgres_ledger_read_fallback_to_sqlite is False


def test_read_mode_diagnostics_endpoint_returns_no_secrets(monkeypatch) -> None:
    _clear_postgres_read_env(monkeypatch)
    monkeypatch.setenv("POSTGRES_LEDGER_READS_ENABLED", "true")
    monkeypatch.setenv("POSTGRES_LEDGER_READ_MODE", "postgres_shadow_candidate")
    monkeypatch.setenv("POSTGRES_LEDGER_READ_ALLOWED_ENDPOINTS", "settlement_history,credits_report")

    client = TestClient(app)
    response = client.get("/postgres-shadow/read-mode")

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "configured_mode": "postgres_shadow_candidate",
        "effective_mode": "postgres_shadow_candidate",
        "reads_enabled": True,
        "fallback_enabled": True,
        "require_shadow_match": True,
        "allowed_endpoints": ["settlement_history", "credits_report"],
    }
    forbidden_keys = {"database_url", "password", "token", "secret", "connection_string"}
    assert forbidden_keys.isdisjoint({key.lower() for key in payload})
