from __future__ import annotations

import re
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping

from payouts.collector.app import sc_node_payout_planner as planner

CORRECTION_STATUS_DRAFT = "draft"
CORRECTION_STATUS_APPLIED = "applied"
CORRECTION_STATUS_CANCELLED = "cancelled"

CORRECTION_DIRECTION_OFFSET_DEBIT = "offset_debit"

_APPLICABLE_CORRECTION_STATUSES = frozenset({CORRECTION_STATUS_DRAFT})

_CORRECTION_MUTATION_TABLES = frozenset({"sc_node_payout_corrections"})

_WALLET_SEND_KEYWORDS = re.compile(
    r"\b("
    r"sendmany|sendtoaddress|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|privkey"
    r")\b",
    re.IGNORECASE,
)

_READONLY_SQL_FORBIDDEN = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE|"
    r"GRANT|REVOKE|VACUUM|CALL"
    r")\b",
    re.IGNORECASE,
)


def assert_no_wallet_send_keywords(text: str) -> None:
    if _WALLET_SEND_KEYWORDS.search(text):
        raise ValueError("text must not contain wallet send or signing keywords")


def _assert_readonly_sql(sql: str) -> None:
    if _READONLY_SQL_FORBIDDEN.search(sql):
        raise ValueError("SQL must be read-only SELECT only")


def _assert_mutation_sql_targets_correction_table_only(sql: str) -> None:
    assert_no_wallet_send_keywords(sql)
    lowered = sql.lower()
    tables = set(
        re.findall(r"\b(?:insert\s+into|update)\s+([a-z0-9_]+)\b", lowered)
    )
    if not tables:
        raise ValueError("mutation SQL must target sc_node_payout_corrections")
    if tables - _CORRECTION_MUTATION_TABLES:
        raise ValueError("mutation SQL must not target non-correction tables")


def normalize_wallet_name(value: str) -> str:
    return planner.normalize_wallet_name(value)


def parse_decimal_amount(value: str, *, field_name: str) -> Decimal:
    return planner.parse_decimal_amount(value, field_name=field_name)


def build_insert_correction_sql() -> str:
    sql = """
INSERT INTO sc_node_payout_corrections (
  sc_node_id,
  wallet_name,
  amount,
  direction,
  reason_code,
  notes,
  related_credit_run_id,
  related_reward_event_id,
  related_txid,
  status,
  created_by
) VALUES (
  %(sc_node_id)s,
  %(wallet_name)s,
  %(amount)s,
  %(direction)s,
  %(reason_code)s,
  %(notes)s,
  %(related_credit_run_id)s,
  %(related_reward_event_id)s,
  %(related_txid)s,
  %(status)s,
  %(created_by)s
)
RETURNING id
""".strip()
    _assert_mutation_sql_targets_correction_table_only(sql)
    return sql


def build_correction_details_sql(correction_id: int) -> str:
    safe_id = int(correction_id)
    sql = f"""
SELECT
  id,
  sc_node_id,
  wallet_name,
  amount,
  direction,
  reason_code,
  notes,
  related_credit_run_id,
  related_payout_plan_id,
  related_reward_event_id,
  related_txid,
  status,
  created_by,
  created_at,
  applied_at,
  cancelled_at
FROM sc_node_payout_corrections
WHERE id = {safe_id}
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_corrections_list_sql() -> str:
    sql = """
SELECT
  id,
  sc_node_id,
  wallet_name,
  amount,
  direction,
  reason_code,
  notes,
  related_credit_run_id,
  related_payout_plan_id,
  related_reward_event_id,
  related_txid,
  status,
  created_by,
  created_at,
  applied_at,
  cancelled_at
FROM sc_node_payout_corrections
ORDER BY created_at DESC, id DESC
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_cancel_correction_sql() -> str:
    sql = """
UPDATE sc_node_payout_corrections
SET
  status = %(status)s,
  cancelled_at = now()
WHERE id = %(correction_id)s
  AND status = 'draft'
RETURNING id
""".strip()
    _assert_mutation_sql_targets_correction_table_only(sql)
    return sql


def build_apply_correction_sql() -> str:
    sql = """
UPDATE sc_node_payout_corrections
SET
  status = %(status)s,
  related_payout_plan_id = %(related_payout_plan_id)s,
  applied_at = now()
WHERE id = %(correction_id)s
  AND status = 'draft'
RETURNING id
""".strip()
    _assert_mutation_sql_targets_correction_table_only(sql)
    return sql


def evaluate_cancel_correction_refusal(
    correction: Mapping[str, Any] | None,
) -> str | None:
    if correction is None:
        return "payout correction not found"
    status = str(correction.get("status"))
    if status == CORRECTION_STATUS_CANCELLED:
        return None
    if status != CORRECTION_STATUS_DRAFT:
        return f"only draft corrections can be cancelled (status={status})"
    return None


def evaluate_correction_for_plan_refusal(
    *,
    correction: Mapping[str, Any] | None,
    wallet_name: str,
    credit_run_id: int,
    sc_node_ids: set[str],
) -> str | None:
    if correction is None:
        return "payout correction not found"
    status = str(correction.get("status"))
    if status == CORRECTION_STATUS_APPLIED:
        return "payout correction already applied"
    if status == CORRECTION_STATUS_CANCELLED:
        return "payout correction is cancelled"
    if status not in _APPLICABLE_CORRECTION_STATUSES:
        return f"payout correction status must be draft (status={status})"
    if str(correction.get("wallet_name")) != wallet_name:
        return "payout correction wallet_name does not match payout plan wallet"
    correction_sc_node_id = str(correction.get("sc_node_id"))
    if correction_sc_node_id not in sc_node_ids:
        return (
            f"payout correction sc_node_id {correction_sc_node_id} "
            "is not in payout plan credits"
        )
    related_credit_run_id = correction.get("related_credit_run_id")
    if related_credit_run_id is not None and int(related_credit_run_id) != int(
        credit_run_id
    ):
        return "payout correction related_credit_run_id does not match credit run"
    amount = _to_decimal(correction.get("amount"))
    if amount <= 0:
        return "payout correction amount must be greater than zero"
    return None


def apply_correction_to_row_amounts(
    *,
    gross_credit_amount: Decimal,
    correction_amount: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    gross = _quantize_amount(gross_credit_amount)
    correction = _quantize_amount(correction_amount)
    if correction > gross:
        raise ValueError(
            f"correction amount {correction} exceeds gross credit amount {gross}"
        )
    net = _quantize_amount(gross - correction)
    if net < 0:
        raise ValueError("net payout amount would be negative after correction")
    return gross, correction, net


def evaluate_correction_amount_refusal(
    *,
    gross_credit_amount: Decimal,
    correction_amount: Decimal,
) -> str | None:
    try:
        apply_correction_to_row_amounts(
            gross_credit_amount=gross_credit_amount,
            correction_amount=correction_amount,
        )
    except ValueError as exc:
        return str(exc)
    return None


def row_to_correction_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _to_int(row["id"]),
        "sc_node_id": str(row["sc_node_id"]),
        "wallet_name": row.get("wallet_name"),
        "amount": _serialize_numeric(_to_decimal(row.get("amount"))),
        "direction": row.get("direction"),
        "reason_code": row.get("reason_code"),
        "notes": row.get("notes"),
        "related_credit_run_id": _optional_int(row.get("related_credit_run_id")),
        "related_payout_plan_id": _optional_int(row.get("related_payout_plan_id")),
        "related_reward_event_id": _optional_int(row.get("related_reward_event_id")),
        "related_txid": row.get("related_txid"),
        "status": row.get("status"),
        "created_by": row.get("created_by"),
        "created_at": _serialize_datetime(row.get("created_at")),
        "applied_at": _serialize_datetime(row.get("applied_at")),
        "cancelled_at": _serialize_datetime(row.get("cancelled_at")),
    }


def _to_decimal(value: object) -> Decimal:
    return planner._to_decimal(value)


def _to_int(value: object) -> int:
    return planner._to_int(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _serialize_numeric(value: Decimal) -> str:
    return planner._serialize_numeric(value)


def _quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000000000001"), rounding=ROUND_DOWN)


def _serialize_datetime(value: object) -> str | None:
    return planner._serialize_datetime(value)
