from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PoolInstanceConfig:
    id: str
    base_url: str
    display_name: str | None = None


@dataclass(frozen=True)
class CollectorSettings:
    database_url: str
    pool_instances: tuple[PoolInstanceConfig, ...]
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


def _parse_pool_instances(raw: str) -> tuple[PoolInstanceConfig, ...]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("POOL_INSTANCES must be valid JSON") from exc

    if not isinstance(payload, list) or not payload:
        raise ConfigError("POOL_INSTANCES must be a non-empty JSON list")

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


def load_settings() -> CollectorSettings:
    database_url = _require_env("DATABASE_URL")
    pool_instances = _parse_pool_instances(_require_env("POOL_INSTANCES"))
    timeout = int(os.environ.get("COLLECTOR_REQUEST_TIMEOUT_SECONDS", "10"))
    clients_limit = int(os.environ.get("COLLECTOR_CLIENTS_PAGE_LIMIT", "100"))
    channels_limit = int(os.environ.get("COLLECTOR_CHANNELS_PAGE_LIMIT", "100"))
    return CollectorSettings(
        database_url=database_url,
        pool_instances=pool_instances,
        request_timeout_seconds=timeout,
        clients_page_limit=clients_limit,
        channels_page_limit=channels_limit,
    )
