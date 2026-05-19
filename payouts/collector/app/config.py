from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PoolInstanceConfig:
    id: str
    base_url: str
    display_name: str | None = None


@dataclass(frozen=True)
class CollectorSettings:
    database_url: str
    env_pool_instances: tuple[PoolInstanceConfig, ...]
    request_timeout_seconds: int = 10
    clients_page_limit: int = 100
    channels_page_limit: int = 100


class ConfigError(ValueError):
    """Raised when required collector configuration is missing or invalid."""


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def parse_env_pool_instances(raw: str | None) -> tuple[PoolInstanceConfig, ...]:
    if raw is None or not raw.strip():
        return ()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("POOL_INSTANCES must be valid JSON") from exc

    if not isinstance(payload, list):
        raise ConfigError("POOL_INSTANCES must be a JSON list")

    instances: list[PoolInstanceConfig] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ConfigError("Each POOL_INSTANCES entry must be an object")
        pool_id = str(item.get("id") or "").strip()
        base_url = str(item.get("base_url") or "").strip().rstrip("/")
        if not pool_id or not base_url:
            raise ConfigError("Each POOL_INSTANCES entry requires id and base_url")
        display_name = str(item.get("display_name") or pool_id).strip() or pool_id
        instances.append(PoolInstanceConfig(id=pool_id, base_url=base_url, display_name=display_name))

    return tuple(instances)


def resolve_pool_instances(
    db_pools: tuple[PoolInstanceConfig, ...] | None,
    env_pools: tuple[PoolInstanceConfig, ...],
) -> tuple[PoolInstanceConfig, ...]:
    """Prefer DB registry; fall back to POOL_INSTANCES env when DB has no active pools."""
    if db_pools:
        return db_pools

    if db_pools is None:
        logger.warning(
            "pool instance registry unavailable; using POOL_INSTANCES env fallback if configured"
        )
    else:
        logger.warning(
            "pool instance registry returned zero active pools; using POOL_INSTANCES env fallback if configured"
        )

    if env_pools:
        return env_pools

    raise ConfigError(
        "no active pool instances in database registry and POOL_INSTANCES env fallback is empty"
    )


def load_settings() -> CollectorSettings:
    database_url = _require_env("DATABASE_URL")
    env_pools = parse_env_pool_instances(os.environ.get("POOL_INSTANCES"))
    timeout = int(os.environ.get("COLLECTOR_REQUEST_TIMEOUT_SECONDS", "10"))
    clients_limit = int(os.environ.get("COLLECTOR_CLIENTS_PAGE_LIMIT", "100"))
    channels_limit = int(os.environ.get("COLLECTOR_CHANNELS_PAGE_LIMIT", "100"))
    return CollectorSettings(
        database_url=database_url,
        env_pool_instances=env_pools,
        request_timeout_seconds=timeout,
        clients_page_limit=clients_limit,
        channels_page_limit=channels_limit,
    )
