from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from payouts.collector.app import (
    payout_addresses,
    reward_events,
    sc_node_credit_ledger,
    sc_node_payout_plan_review,
    sc_node_payout_planner,
    sc_node_payout_production_executor,
    sc_node_payout_production_preflight,
    sc_node_payout_test_executor,
)

MIN_UNMAPPED_LIMIT = 1
MAX_UNMAPPED_LIMIT = 500
DEFAULT_UNMAPPED_LIMIT = 20

_READONLY_SQL_FORBIDDEN = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE|"
    r"GRANT|REVOKE|VACUUM|CALL"
    r")\b",
    re.IGNORECASE,
)


def assert_readonly_sql(sql: str) -> None:
    """Raise ValueError if SQL appears to mutate data or schema."""
    if _READONLY_SQL_FORBIDDEN.search(sql):
        raise ValueError("SQL must be read-only SELECT only")


def clamp_unmapped_limit(limit: int) -> int:
    """Clamp unmapped-identities limit to a safe bounded range."""
    return max(MIN_UNMAPPED_LIMIT, min(MAX_UNMAPPED_LIMIT, int(limit)))


def build_pool_instances_sql() -> str:
    sql = """
SELECT
  id,
  display_name,
  monitoring_base_url,
  status,
  monitoring_enabled,
  created_at,
  updated_at
FROM pool_instances
ORDER BY id
""".strip()
    assert_readonly_sql(sql)
    return sql


def build_sc_nodes_sql() -> str:
    sql = """
SELECT
  id,
  display_name,
  status,
  payout_enabled,
  created_at,
  updated_at
FROM sc_nodes
ORDER BY id
""".strip()
    assert_readonly_sql(sql)
    return sql


def build_identity_mappings_sql() -> str:
    sql = """
SELECT
  m.id,
  m.sc_node_id,
  n.display_name AS sc_node_display_name,
  m.match_type,
  m.match_value,
  m.status,
  m.created_at
FROM sc_node_identity_mappings m
LEFT JOIN sc_nodes n ON n.id = m.sc_node_id
ORDER BY m.sc_node_id, m.match_type, m.match_value, m.id
""".strip()
    assert_readonly_sql(sql)
    return sql


def build_unmapped_identities_sql(limit: int = DEFAULT_UNMAPPED_LIMIT) -> str:
    safe_limit = clamp_unmapped_limit(limit)
    sql = f"""
SELECT
  user_identity,
  COUNT(*)::bigint AS delta_rows,
  COALESCE(SUM(accepted_delta), 0) AS accepted_delta_total,
  COALESCE(SUM(work_delta), 0) AS work_delta_total,
  MIN(observed_from) AS first_observed_at,
  MAX(observed_to) AS last_observed_at
FROM pool_share_work_deltas
WHERE sc_node_id IS NULL
GROUP BY user_identity
ORDER BY delta_rows DESC, user_identity ASC
LIMIT {safe_limit}
""".strip()
    assert_readonly_sql(sql)
    return sql


def build_payout_addresses_sql() -> str:
    return payout_addresses.build_sc_node_payout_addresses_sql(include_inactive=True)


def row_to_payout_address_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return payout_addresses.row_to_payout_address_dict(row)


def build_reward_events_sql(maturity_status: str | None = None) -> str:
    return reward_events.build_reward_events_sql(
        include_raw=False,
        maturity_status=maturity_status,
    )


def row_to_reward_event_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return reward_events.row_to_reward_event_dict(row, include_raw=False)


def build_credit_runs_sql() -> str:
    return sc_node_credit_ledger.build_credit_runs_sql()


def build_credit_run_details_sql(credit_run_id: int) -> str:
    return sc_node_credit_ledger.build_credit_run_details_sql(credit_run_id)


def build_credit_run_credits_sql(credit_run_id: int) -> str:
    return sc_node_credit_ledger.build_credit_run_credits_sql(credit_run_id)


def build_credit_run_events_sql(credit_run_id: int) -> str:
    return sc_node_credit_ledger.build_credit_run_events_sql(credit_run_id)


def row_to_credit_run_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return sc_node_credit_ledger.row_to_credit_run_dict(row)


def row_to_credit_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return sc_node_credit_ledger.row_to_credit_dict(row)


def row_to_credit_run_event_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return sc_node_credit_ledger.row_to_credit_run_event_dict(row)


def build_payout_plans_sql() -> str:
    return sc_node_payout_plan_review.build_payout_plans_list_sql()


def build_payout_plan_details_sql(payout_plan_id: int) -> str:
    return sc_node_payout_plan_review.build_payout_plan_details_sql(payout_plan_id)


def build_payout_plan_rows_sql(payout_plan_id: int) -> str:
    return sc_node_payout_planner.build_payout_plan_rows_sql(payout_plan_id)


def row_to_payout_plan_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return sc_node_payout_plan_review.row_to_payout_plan_dict(row)


def row_to_payout_plan_row_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return sc_node_payout_planner.row_to_payout_plan_row_dict(row)


def build_payout_test_executions_sql() -> str:
    return sc_node_payout_test_executor.build_test_executions_list_sql()


def build_payout_test_execution_details_sql(test_execution_id: int) -> str:
    return sc_node_payout_test_executor.build_test_execution_details_sql(test_execution_id)


def build_payout_test_execution_rows_sql(test_execution_id: int) -> str:
    return sc_node_payout_test_executor.build_test_execution_rows_sql(test_execution_id)


def row_to_payout_test_execution_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return sc_node_payout_test_executor.row_to_test_execution_dict(row)


def row_to_payout_test_execution_row_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return sc_node_payout_test_executor.row_to_test_execution_row_dict(row)


def build_production_preflights_sql() -> str:
    return sc_node_payout_production_preflight.build_production_preflights_sql()


def build_production_preflight_details_sql(production_preflight_id: int) -> str:
    return sc_node_payout_production_preflight.build_production_preflight_details_sql(
        production_preflight_id
    )


def build_production_preflight_rows_sql(production_preflight_id: int) -> str:
    return sc_node_payout_production_preflight.build_production_preflight_rows_sql(
        production_preflight_id
    )


def row_to_production_preflight_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return sc_node_payout_production_preflight.row_to_production_preflight_dict(row)


def row_to_production_preflight_row_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return sc_node_payout_production_preflight.row_to_production_preflight_row_dict(row)


def build_production_executions_sql() -> str:
    return sc_node_payout_production_executor.build_production_executions_sql()


def build_production_execution_details_sql(production_execution_id: int) -> str:
    return sc_node_payout_production_executor.build_production_execution_details_sql(
        production_execution_id
    )


def build_production_execution_rows_sql(production_execution_id: int) -> str:
    return sc_node_payout_production_executor.build_production_execution_rows_sql(
        production_execution_id
    )


def row_to_production_execution_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return sc_node_payout_production_executor.row_to_production_execution_dict(row)


def row_to_production_execution_row_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return sc_node_payout_production_executor.row_to_production_execution_row_dict(row)


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _serialize_numeric(value: Decimal) -> str:
    return format(value, "f")


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


def _to_bool(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return bool(value)


def row_to_pool_instance_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "display_name": row.get("display_name"),
        "monitoring_base_url": row.get("monitoring_base_url"),
        "status": row.get("status"),
        "monitoring_enabled": _to_bool(row.get("monitoring_enabled")),
        "created_at": _serialize_datetime(row.get("created_at")),
        "updated_at": _serialize_datetime(row.get("updated_at")),
    }


def row_to_sc_node_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "display_name": row.get("display_name"),
        "status": row.get("status"),
        "payout_enabled": _to_bool(row.get("payout_enabled")),
        "created_at": _serialize_datetime(row.get("created_at")),
        "updated_at": _serialize_datetime(row.get("updated_at")),
    }


def row_to_identity_mapping_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _to_int(row["id"]),
        "sc_node_id": str(row["sc_node_id"]),
        "sc_node_display_name": row.get("sc_node_display_name"),
        "match_type": row.get("match_type"),
        "match_value": row.get("match_value"),
        "status": row.get("status"),
        "created_at": _serialize_datetime(row.get("created_at")),
    }


def row_to_unmapped_identity_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "user_identity": str(row["user_identity"]),
        "delta_rows": _to_int(row["delta_rows"]),
        "accepted_delta_total": _serialize_numeric(_to_decimal(row["accepted_delta_total"])),
        "work_delta_total": _serialize_numeric(_to_decimal(row["work_delta_total"])),
        "first_observed_at": _serialize_datetime(row.get("first_observed_at")),
        "last_observed_at": _serialize_datetime(row.get("last_observed_at")),
    }
