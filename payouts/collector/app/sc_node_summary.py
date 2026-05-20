from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping


@dataclass(frozen=True)
class ScNodeWorkSummary:
    sc_node_id: str
    display_name: str | None
    status: str | None
    payout_enabled: bool
    accepted_delta_total: Decimal
    work_delta_total: Decimal
    delta_rows: int
    first_observed_at: datetime | None
    last_observed_at: datetime | None


@dataclass(frozen=True)
class UnmappedWorkSummary:
    accepted_delta_total: Decimal
    work_delta_total: Decimal
    delta_rows: int
    first_observed_at: datetime | None
    last_observed_at: datetime | None


def build_sc_node_summary_sql() -> str:
    """Return read-only SQL aggregating mapped telemetry by sc_node_id."""
    return """
SELECT
  d.sc_node_id,
  n.display_name,
  n.status,
  n.payout_enabled,
  COALESCE(SUM(d.accepted_delta), 0) AS accepted_delta_total,
  COALESCE(SUM(d.work_delta), 0) AS work_delta_total,
  COUNT(*)::bigint AS delta_rows,
  MIN(d.observed_from) AS first_observed_at,
  MAX(d.observed_to) AS last_observed_at
FROM pool_share_work_deltas d
LEFT JOIN sc_nodes n ON n.id = d.sc_node_id
WHERE d.sc_node_id IS NOT NULL
GROUP BY d.sc_node_id, n.display_name, n.status, n.payout_enabled
ORDER BY work_delta_total DESC, d.sc_node_id
""".strip()


def build_unmapped_summary_sql() -> str:
    """Return read-only SQL aggregating telemetry with no sc_node_id mapping."""
    return """
SELECT
  COALESCE(SUM(accepted_delta), 0) AS accepted_delta_total,
  COALESCE(SUM(work_delta), 0) AS work_delta_total,
  COUNT(*)::bigint AS delta_rows,
  MIN(observed_from) AS first_observed_at,
  MAX(observed_to) AS last_observed_at
FROM pool_share_work_deltas
WHERE sc_node_id IS NULL
""".strip()


def _to_decimal(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _to_int(value: object) -> int:
    if value is None:
        return 0
    return int(value)


def _serialize_numeric(value: Decimal) -> str:
    return format(value, "f")


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _to_bool(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return bool(value)


def row_to_sc_node_summary_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a DB row into a safe JSON-serializable SC-node summary dict."""
    accepted_total = _to_decimal(row["accepted_delta_total"])
    work_total = _to_decimal(row["work_delta_total"])
    return {
        "sc_node_id": str(row["sc_node_id"]),
        "display_name": row.get("display_name"),
        "status": row.get("status"),
        "payout_enabled": _to_bool(row.get("payout_enabled")),
        "accepted_delta_total": _serialize_numeric(accepted_total),
        "work_delta_total": _serialize_numeric(work_total),
        "delta_rows": _to_int(row["delta_rows"]),
        "first_observed_at": _serialize_datetime(row.get("first_observed_at")),
        "last_observed_at": _serialize_datetime(row.get("last_observed_at")),
    }


def row_to_unmapped_summary_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a DB row into a safe JSON-serializable unmapped work summary dict."""
    accepted_total = _to_decimal(row["accepted_delta_total"])
    work_total = _to_decimal(row["work_delta_total"])
    return {
        "accepted_delta_total": _serialize_numeric(accepted_total),
        "work_delta_total": _serialize_numeric(work_total),
        "delta_rows": _to_int(row["delta_rows"]),
        "first_observed_at": _serialize_datetime(row.get("first_observed_at")),
        "last_observed_at": _serialize_datetime(row.get("last_observed_at")),
    }
