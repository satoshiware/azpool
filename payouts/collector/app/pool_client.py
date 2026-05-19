from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import requests

from payouts.collector.app.identity import parse_sc_node_id


class PoolMonitoringError(RuntimeError):
    """Raised when pool_sv2 monitoring API calls fail."""


@dataclass(frozen=True)
class NormalizedChannel:
    client_id: int
    channel_type: str
    channel_id: int
    user_identity: str
    sc_node_id: str | None
    shares_accepted: Decimal
    share_work_sum: Decimal
    last_share_sequence_number: int | None
    blocks_found: Decimal


def _extract_items(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    data = payload.get("data")
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    return []


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _channel_payload_root(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def fetch_health(base_url: str, *, timeout_seconds: int = 10) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/v1/health"
    try:
        response = requests.get(url, timeout=timeout_seconds)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise PoolMonitoringError(f"health check failed for {base_url}: {exc}") from exc
    except ValueError as exc:
        raise PoolMonitoringError(f"health check returned invalid JSON for {base_url}") from exc

    if not isinstance(payload, dict):
        raise PoolMonitoringError(f"health check payload must be an object for {base_url}")
    return payload


def fetch_clients(
    base_url: str,
    *,
    offset: int = 0,
    limit: int = 100,
    timeout_seconds: int = 10,
) -> list[dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/api/v1/clients"
    try:
        response = requests.get(
            url,
            params={"offset": offset, "limit": limit},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise PoolMonitoringError(f"clients list failed for {base_url}: {exc}") from exc
    except ValueError as exc:
        raise PoolMonitoringError(f"clients list returned invalid JSON for {base_url}") from exc

    return _extract_items(payload, "clients", "items", "results")


def fetch_client_channels(
    base_url: str,
    client_id: int,
    *,
    offset: int = 0,
    limit: int = 100,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/v1/clients/{client_id}/channels"
    try:
        response = requests.get(
            url,
            params={"offset": offset, "limit": limit},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise PoolMonitoringError(
            f"channels fetch failed for {base_url} client_id={client_id}: {exc}"
        ) from exc
    except ValueError as exc:
        raise PoolMonitoringError(
            f"channels fetch returned invalid JSON for {base_url} client_id={client_id}"
        ) from exc

    if not isinstance(payload, dict):
        raise PoolMonitoringError(
            f"channels payload must be an object for {base_url} client_id={client_id}"
        )
    return payload


def normalize_channels(client_id: int, payload: dict[str, Any]) -> list[NormalizedChannel]:
    root = _channel_payload_root(payload)
    normalized: list[NormalizedChannel] = []

    for channel_type in ("extended", "standard"):
        key = f"{channel_type}_channels"
        channels = root.get(key)
        if not isinstance(channels, list):
            continue

        for channel in channels:
            if not isinstance(channel, dict):
                continue

            channel_id = _to_int(channel.get("channel_id"))
            if channel_id is None:
                continue

            user_identity = str(
                channel.get("user_identity")
                or channel.get("authorized_worker_name")
                or ""
            ).strip()
            if not user_identity:
                continue

            shares_accepted = channel.get("shares_accepted")
            if shares_accepted is None:
                shares_accepted = channel.get("shares_acknowledged")

            normalized.append(
                NormalizedChannel(
                    client_id=client_id,
                    channel_type=channel_type,
                    channel_id=channel_id,
                    user_identity=user_identity,
                    sc_node_id=parse_sc_node_id(user_identity),
                    shares_accepted=_to_decimal(shares_accepted),
                    share_work_sum=_to_decimal(channel.get("share_work_sum")),
                    last_share_sequence_number=_to_int(channel.get("last_share_sequence_number")),
                    blocks_found=_to_decimal(channel.get("blocks_found")),
                )
            )

    return normalized
