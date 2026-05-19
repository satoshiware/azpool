from __future__ import annotations

import sys
from pathlib import Path

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

import pytest

from payouts.collector.app.config import (
    ConfigError,
    PoolInstanceConfig,
    parse_env_pool_instances,
    resolve_pool_instances,
)
from payouts.collector.app.db import is_active_registry_pool, pool_instance_config_from_row


ENV_POOLS = (
    PoolInstanceConfig(id="pool01", base_url="http://10.10.70.131:9090", display_name="Pool 01"),
    PoolInstanceConfig(id="pool02", base_url="http://10.10.70.43:9090", display_name="Pool 02"),
)

DB_POOLS = (
    PoolInstanceConfig(id="pool03", base_url="http://10.10.70.99:9090", display_name="Pool 03"),
)


def test_pool_instance_config_from_row_maps_monitoring_base_url() -> None:
    config = pool_instance_config_from_row(
        pool_id="pool01",
        display_name="Pool 01",
        monitoring_base_url="http://10.10.70.131:9090/",
        status="active",
        monitoring_enabled=True,
    )
    assert config == PoolInstanceConfig(
        id="pool01",
        base_url="http://10.10.70.131:9090",
        display_name="Pool 01",
    )


def test_parse_env_pool_instances_missing_returns_empty() -> None:
    assert parse_env_pool_instances(None) == ()
    assert parse_env_pool_instances("") == ()


def test_resolve_pool_instances_db_wins_over_env() -> None:
    assert resolve_pool_instances(DB_POOLS, ENV_POOLS) == DB_POOLS


def test_resolve_pool_instances_env_fallback_when_db_empty() -> None:
    assert resolve_pool_instances((), ENV_POOLS) == ENV_POOLS


def test_resolve_pool_instances_env_fallback_when_db_unavailable() -> None:
    assert resolve_pool_instances(None, ENV_POOLS) == ENV_POOLS


def test_resolve_pool_instances_fails_when_both_empty() -> None:
    with pytest.raises(ConfigError, match="no active pool instances"):
        resolve_pool_instances((), ())


@pytest.mark.parametrize(
    ("status", "monitoring_enabled", "monitoring_base_url", "expected"),
    [
        ("active", True, "http://10.10.70.131:9090", True),
        ("inactive", True, "http://10.10.70.131:9090", False),
        ("active", False, "http://10.10.70.131:9090", False),
        ("active", True, None, False),
        ("active", True, "  ", False),
    ],
)
def test_is_active_registry_pool_filters(
    status: str,
    monitoring_enabled: bool,
    monitoring_base_url: str | None,
    expected: bool,
) -> None:
    assert is_active_registry_pool(
        status=status,
        monitoring_enabled=monitoring_enabled,
        monitoring_base_url=monitoring_base_url,
    ) is expected


def test_pool_instance_config_from_row_ignores_inactive() -> None:
    assert (
        pool_instance_config_from_row(
            pool_id="pool01",
            display_name="Pool 01",
            monitoring_base_url="http://10.10.70.131:9090",
            status="inactive",
            monitoring_enabled=True,
        )
        is None
    )


def test_load_settings_without_pool_instances_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.delenv("POOL_INSTANCES", raising=False)

    from payouts.collector.app.config import load_settings

    settings = load_settings()
    assert settings.database_url == "postgresql://example"
    assert settings.env_pool_instances == ()
