from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

VALID_PAYOUT_ADDRESS_STATUSES = frozenset(
    {"pending_verification", "active", "inactive", "revoked"}
)
VALID_PAYOUT_ADDRESS_SOURCES = frozenset({"manual", "imported", "wallet", "api"})

_READONLY_SQL_FORBIDDEN = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE|"
    r"GRANT|REVOKE|VACUUM|CALL"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScNodePayoutAddress:
    id: int
    sc_node_id: str
    sc_node_display_name: str | None
    payout_address: str
    label: str | None
    address_source: str
    status: str
    is_default: bool
    verified_at: datetime | None
    retired_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None


def assert_readonly_sql(sql: str) -> None:
    """Raise ValueError if SQL appears to mutate data or schema."""
    if _READONLY_SQL_FORBIDDEN.search(sql):
        raise ValueError("SQL must be read-only SELECT only")


def normalize_payout_address(value: str) -> str:
    """Trim whitespace from a payout address string."""
    return value.strip()


def validate_payout_address_record(
    *,
    sc_node_id: str,
    payout_address: str,
    status: str,
    address_source: str,
    is_default: bool = False,
) -> None:
    """Validate registry field values without wallet RPC or on-chain checks."""
    if not str(sc_node_id).strip():
        raise ValueError("sc_node_id is required")
    normalized = normalize_payout_address(payout_address)
    if not normalized:
        raise ValueError("payout_address cannot be empty")
    if status not in VALID_PAYOUT_ADDRESS_STATUSES:
        raise ValueError(f"invalid status: {status}")
    if address_source not in VALID_PAYOUT_ADDRESS_SOURCES:
        raise ValueError(f"invalid address_source: {address_source}")
    if is_default and status != "active":
        raise ValueError("is_default requires status active")


def build_manual_register_record(
    *,
    sc_node_id: str,
    payout_address: str,
    status: str = "pending_verification",
    address_source: str = "manual",
    is_default: bool = False,
    label: str | None = None,
) -> dict[str, str | bool | None]:
    """Build validated metadata for manual INSERT (registry only; no DB write)."""
    validate_payout_address_record(
        sc_node_id=sc_node_id,
        payout_address=payout_address,
        status=status,
        address_source=address_source,
        is_default=is_default,
    )
    return {
        "sc_node_id": str(sc_node_id).strip(),
        "payout_address": normalize_payout_address(payout_address),
        "status": status,
        "address_source": address_source,
        "is_default": is_default,
        "label": label,
    }


def build_sc_node_payout_addresses_sql(*, include_inactive: bool = True) -> str:
    inactive_filter = ""
    if not include_inactive:
        inactive_filter = "\nWHERE a.status IN ('pending_verification', 'active')"
    sql = f"""
SELECT
  a.id,
  a.sc_node_id,
  n.display_name AS sc_node_display_name,
  a.payout_address,
  a.label,
  a.address_source,
  a.status,
  a.is_default,
  a.verified_at,
  a.retired_at,
  a.created_at,
  a.updated_at
FROM sc_node_payout_addresses a
LEFT JOIN sc_nodes n ON n.id = a.sc_node_id{inactive_filter}
ORDER BY a.sc_node_id, a.is_default DESC, a.status, a.id
""".strip()
    assert_readonly_sql(sql)
    return sql


def build_active_default_payout_addresses_sql() -> str:
    sql = """
SELECT
  a.id,
  a.sc_node_id,
  n.display_name AS sc_node_display_name,
  a.payout_address,
  a.label,
  a.address_source,
  a.status,
  a.is_default,
  a.verified_at,
  a.retired_at,
  a.created_at,
  a.updated_at
FROM sc_node_payout_addresses a
LEFT JOIN sc_nodes n ON n.id = a.sc_node_id
WHERE a.is_default = true
  AND a.status = 'active'
ORDER BY a.sc_node_id, a.id
""".strip()
    assert_readonly_sql(sql)
    return sql


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _to_int(value: object) -> int:
    if value is None:
        return 0
    return int(value)


def _to_bool(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return bool(value)


def row_to_payout_address_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a DB row into a safe JSON-serializable payout address dict."""
    return {
        "id": _to_int(row["id"]),
        "sc_node_id": str(row["sc_node_id"]),
        "sc_node_display_name": row.get("sc_node_display_name"),
        "payout_address": str(row["payout_address"]),
        "label": row.get("label"),
        "address_source": row.get("address_source"),
        "status": row.get("status"),
        "is_default": _to_bool(row.get("is_default")),
        "verified_at": _serialize_datetime(row.get("verified_at")),
        "retired_at": _serialize_datetime(row.get("retired_at")),
        "created_at": _serialize_datetime(row.get("created_at")),
        "updated_at": _serialize_datetime(row.get("updated_at")),
    }
