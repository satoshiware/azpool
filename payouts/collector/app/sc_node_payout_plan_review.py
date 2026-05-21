from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from payouts.collector.app import sc_node_payout_planner as planner

PLAN_STATUS_DRAFT = "draft"
PLAN_STATUS_APPROVED = "approved"
PLAN_STATUS_CANCELLED = "cancelled"
ROW_STATUS_DRAFT = "draft"
ROW_STATUS_APPROVED = "approved"
ROW_STATUS_CANCELLED = "cancelled"

PREFLIGHT_STATUS_ALLOWED = "allowed"
PREFLIGHT_STATUS_REFUSED = "refused"

_REVIEW_UPDATE_TABLES = frozenset(
    {"sc_node_payout_plans", "sc_node_payout_plan_rows"}
)

_WALLET_SEND_KEYWORDS = re.compile(
    r"\b("
    r"sendmany|sendtoaddress|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|privkey"
    r")\b",
    re.IGNORECASE,
)

_PAYOUT_PLAN_SELECT_COLUMNS = """
  id,
  credit_run_id,
  wallet_name,
  status,
  reserve_fraction,
  trusted_balance_snapshot,
  reserve_amount,
  max_spendable_amount,
  planned_amount_total,
  row_count,
  notes,
  approved_at,
  approved_by,
  approval_note,
  approval_confirmation_hash,
  preflight_checked_at,
  preflight_status,
  preflight_note,
  cancelled_at,
  cancelled_by,
  cancellation_note,
  created_at,
  updated_at
""".strip()


@dataclass(frozen=True)
class PreflightResult:
    payout_plan_id: int
    preflight_allowed: bool
    refusal_reason: str | None
    trusted_balance_current: Decimal
    reserve_fraction_current: Decimal
    current_reserve_amount: Decimal
    current_max_spendable_amount: Decimal
    planned_amount_total: Decimal
    row_count: int
    rows: tuple[dict[str, Any], ...]


def assert_no_wallet_send_keywords(text: str) -> None:
    if _WALLET_SEND_KEYWORDS.search(text):
        raise ValueError("text must not contain wallet send or signing keywords")


def build_approval_confirmation_phrase(payout_plan_id: int) -> str:
    return f"APPROVE PAYOUT PLAN {int(payout_plan_id)} NO SEND"


def hash_approval_confirmation(confirmation: str) -> str:
    normalized = confirmation.strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def verify_approval_confirmation(confirmation: str, payout_plan_id: int) -> bool:
    expected = build_approval_confirmation_phrase(payout_plan_id)
    return confirmation.strip() == expected


def build_payout_plan_for_review_sql(payout_plan_id: int) -> str:
    safe_id = int(payout_plan_id)
    sql = f"""
SELECT
{_PAYOUT_PLAN_SELECT_COLUMNS}
FROM sc_node_payout_plans
WHERE id = {safe_id}
""".strip()
    planner._assert_readonly_sql(sql)
    return sql


def build_payout_plan_rows_for_review_sql(payout_plan_id: int) -> str:
    return planner.build_payout_plan_rows_sql(payout_plan_id)


def build_payout_plans_list_sql() -> str:
    sql = f"""
SELECT
{_PAYOUT_PLAN_SELECT_COLUMNS}
FROM sc_node_payout_plans
ORDER BY created_at DESC, id DESC
""".strip()
    planner._assert_readonly_sql(sql)
    return sql


def build_payout_plan_details_sql(payout_plan_id: int) -> str:
    return build_payout_plan_for_review_sql(payout_plan_id)


def _assert_review_update_sql(sql: str) -> None:
    assert_no_wallet_send_keywords(sql)
    lowered = sql.lower()
    if "update" not in lowered:
        raise ValueError("review SQL must be UPDATE only")
    for token in re.findall(r"\bupdate\s+([a-z0-9_]+)\b", lowered):
        if token not in _REVIEW_UPDATE_TABLES:
            raise ValueError(f"review UPDATE must not target table: {token}")


def build_update_approve_plan_sql() -> str:
    sql = """
UPDATE sc_node_payout_plans
SET status = 'approved',
    approved_at = now(),
    approved_by = %(approved_by)s,
    approval_note = %(approval_note)s,
    approval_confirmation_hash = %(approval_confirmation_hash)s,
    updated_at = now()
WHERE id = %(payout_plan_id)s
  AND status = 'draft'
RETURNING id
""".strip()
    _assert_review_update_sql(sql)
    return sql


def build_update_approve_rows_sql() -> str:
    sql = """
UPDATE sc_node_payout_plan_rows
SET row_status = 'approved',
    updated_at = now()
WHERE payout_plan_id = %(payout_plan_id)s
  AND row_status = 'draft'
""".strip()
    _assert_review_update_sql(sql)
    return sql


def build_update_cancel_plan_sql() -> str:
    sql = """
UPDATE sc_node_payout_plans
SET status = 'cancelled',
    cancelled_at = now(),
    cancelled_by = %(cancelled_by)s,
    cancellation_note = %(cancellation_note)s,
    updated_at = now()
WHERE id = %(payout_plan_id)s
  AND status IN ('draft', 'approved')
RETURNING id
""".strip()
    _assert_review_update_sql(sql)
    return sql


def build_update_cancel_rows_sql() -> str:
    sql = """
UPDATE sc_node_payout_plan_rows
SET row_status = 'cancelled',
    updated_at = now()
WHERE payout_plan_id = %(payout_plan_id)s
  AND row_status IN ('draft', 'approved')
""".strip()
    _assert_review_update_sql(sql)
    return sql


def build_update_preflight_plan_sql() -> str:
    sql = """
UPDATE sc_node_payout_plans
SET preflight_checked_at = now(),
    preflight_status = %(preflight_status)s,
    preflight_note = %(preflight_note)s,
    updated_at = now()
WHERE id = %(payout_plan_id)s
RETURNING id
""".strip()
    _assert_review_update_sql(sql)
    return sql


def evaluate_address_drift(
    *,
    sc_node_id: str,
    frozen_address: str,
    registry_address: str | None,
) -> str | None:
    frozen = frozen_address.strip()
    current = (registry_address or "").strip()
    if not current:
        return f"missing active/default payout address for {sc_node_id}"
    if frozen != current:
        return (
            f"payout address drift for {sc_node_id}: "
            f"plan has {frozen}, registry has {current}"
        )
    return None


def collect_address_drift_refusals(
    plan_rows: list[Mapping[str, Any]],
    address_lookup: dict[str, list[Mapping[str, Any]]],
) -> list[str]:
    refusals: list[str] = []
    for row in plan_rows:
        sc_node_id = str(row["sc_node_id"])
        frozen = str(row["payout_address"])
        address_rows = address_lookup.get(sc_node_id, [])
        registry_address, lookup_refusal = planner.resolve_active_default_payout_address(
            address_rows,
            sc_node_id=sc_node_id,
        )
        if lookup_refusal:
            refusals.append(lookup_refusal)
            continue
        drift = evaluate_address_drift(
            sc_node_id=sc_node_id,
            frozen_address=frozen,
            registry_address=registry_address,
        )
        if drift:
            refusals.append(drift)
    return refusals


def evaluate_approve_refusal(
    *,
    plan: Mapping[str, Any] | None,
    plan_rows: list[Mapping[str, Any]],
    address_lookup: dict[str, list[Mapping[str, Any]]],
    confirmation: str,
    payout_plan_id: int,
) -> str | None:
    if plan is None:
        return "payout plan not found"
    if not verify_approval_confirmation(confirmation, payout_plan_id):
        return f"confirmation must be exactly: {build_approval_confirmation_phrase(payout_plan_id)}"
    if str(plan.get("status")) != PLAN_STATUS_DRAFT:
        return "payout plan status must be draft"
    row_count = planner._to_int(plan.get("row_count"))
    if row_count < 1:
        return "payout plan row_count must be at least 1"
    planned = planner._to_decimal(plan.get("planned_amount_total"))
    if planned <= 0:
        return "planned_amount_total must be greater than zero"
    max_spendable = planner._to_decimal(plan.get("max_spendable_amount"))
    spend_refusal = planner.evaluate_spend_cap_refusal(
        planned_amount_total=planned,
        max_spendable_amount=max_spendable,
    )
    if spend_refusal:
        return spend_refusal
    for row in plan_rows:
        if str(row.get("row_status")) != ROW_STATUS_DRAFT:
            return "all payout plan rows must be draft"
    drift_refusals = collect_address_drift_refusals(plan_rows, address_lookup)
    if drift_refusals:
        return "; ".join(drift_refusals)
    return None


def evaluate_cancel_refusal(
    *,
    plan: Mapping[str, Any] | None,
    reason: str,
) -> str | None:
    if plan is None:
        return "payout plan not found"
    if not str(reason).strip():
        return "cancellation reason is required"
    status = str(plan.get("status"))
    if status not in {PLAN_STATUS_DRAFT, PLAN_STATUS_APPROVED}:
        return "payout plan can only be cancelled from draft or approved"
    return None


def evaluate_preflight_refusal(
    *,
    plan: Mapping[str, Any] | None,
    plan_rows: list[Mapping[str, Any]],
    address_lookup: dict[str, list[Mapping[str, Any]]],
    trusted_balance_current: Decimal,
    reserve_fraction_current: Decimal,
) -> str | None:
    if plan is None:
        return "payout plan not found"
    if str(plan.get("status")) != PLAN_STATUS_APPROVED:
        return "payout plan status must be approved for preflight"
    planned = planner._to_decimal(plan.get("planned_amount_total"))
    current_reserve = planner.compute_reserve_amount(
        trusted_balance_current,
        reserve_fraction_current,
    )
    current_max = planner.compute_max_spendable_amount(
        trusted_balance_current,
        current_reserve,
    )
    spend_refusal = planner.evaluate_spend_cap_refusal(
        planned_amount_total=planned,
        max_spendable_amount=current_max,
    )
    if spend_refusal:
        return spend_refusal
    drift_refusals = collect_address_drift_refusals(plan_rows, address_lookup)
    if drift_refusals:
        return "; ".join(drift_refusals)
    return None


def build_preflight_result(
    *,
    payout_plan_id: int,
    plan: Mapping[str, Any],
    plan_rows: list[Mapping[str, Any]],
    trusted_balance_current: Decimal,
    reserve_fraction_current: Decimal,
    address_lookup: dict[str, list[Mapping[str, Any]]],
) -> PreflightResult:
    refusal = evaluate_preflight_refusal(
        plan=plan,
        plan_rows=plan_rows,
        address_lookup=address_lookup,
        trusted_balance_current=trusted_balance_current,
        reserve_fraction_current=reserve_fraction_current,
    )
    current_reserve = planner.compute_reserve_amount(
        trusted_balance_current,
        reserve_fraction_current,
    )
    current_max = planner.compute_max_spendable_amount(
        trusted_balance_current,
        current_reserve,
    )
    rows = [planner.row_to_payout_plan_row_dict(row) for row in plan_rows]
    return PreflightResult(
        payout_plan_id=payout_plan_id,
        preflight_allowed=refusal is None,
        refusal_reason=refusal,
        trusted_balance_current=trusted_balance_current,
        reserve_fraction_current=reserve_fraction_current,
        current_reserve_amount=current_reserve,
        current_max_spendable_amount=current_max,
        planned_amount_total=planner._to_decimal(plan.get("planned_amount_total")),
        row_count=len(plan_rows),
        rows=tuple(rows),
    )


def preflight_result_to_dict(result: PreflightResult) -> dict[str, Any]:
    return {
        "payout_plan_id": result.payout_plan_id,
        "preflight_allowed": result.preflight_allowed,
        "refusal_reason": result.refusal_reason,
        "trusted_balance_current": planner._serialize_numeric(
            result.trusted_balance_current
        ),
        "reserve_fraction_current": planner._serialize_numeric(
            result.reserve_fraction_current
        ),
        "current_reserve_amount": planner._serialize_numeric(
            result.current_reserve_amount
        ),
        "current_max_spendable_amount": planner._serialize_numeric(
            result.current_max_spendable_amount
        ),
        "planned_amount_total": planner._serialize_numeric(result.planned_amount_total),
        "row_count": result.row_count,
        "rows": list(result.rows),
        "accounting_note": (
            "no-send preflight only; not wallet execution or spend authorization"
        ),
    }


def row_to_payout_plan_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    base = planner.row_to_payout_plan_dict(row)
    base.update(
        {
            "approved_at": planner._serialize_datetime(row.get("approved_at")),
            "approved_by": row.get("approved_by"),
            "approval_note": row.get("approval_note"),
            "approval_confirmation_hash": row.get("approval_confirmation_hash"),
            "preflight_checked_at": planner._serialize_datetime(
                row.get("preflight_checked_at")
            ),
            "preflight_status": row.get("preflight_status"),
            "preflight_note": row.get("preflight_note"),
            "cancelled_at": planner._serialize_datetime(row.get("cancelled_at")),
            "cancelled_by": row.get("cancelled_by"),
            "cancellation_note": row.get("cancellation_note"),
        }
    )
    return base
