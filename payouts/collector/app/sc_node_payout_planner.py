from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping

DEFAULT_RESERVE_FRACTION = Decimal("0.50")
PLAN_STATUSES = frozenset({"draft", "reviewed", "void", "executed"})
ROW_STATUSES = frozenset({"draft", "reviewed", "void", "executed"})
REQUIRED_CREDIT_RUN_STATUS = "draft"
REQUIRED_MATURITY_STATUS = "mature"

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

_PLAN_INSERT_TABLES = frozenset(
    {"sc_node_payout_plans", "sc_node_payout_plan_rows"}
)


@dataclass(frozen=True)
class PayoutPlanRowPreview:
    credit_id: int
    sc_node_id: str
    sc_node_display_name: str | None
    payout_address: str
    payout_amount: Decimal


@dataclass(frozen=True)
class PayoutPlanPreview:
    credit_run_id: int
    wallet_name: str
    reserve_fraction: Decimal
    trusted_balance_snapshot: Decimal
    reserve_amount: Decimal
    max_spendable_amount: Decimal
    planned_amount_total: Decimal
    row_count: int
    rows: tuple[PayoutPlanRowPreview, ...]
    plan_allowed: bool
    refusal_reason: str | None


def assert_no_wallet_send_keywords(text: str) -> None:
    if _WALLET_SEND_KEYWORDS.search(text):
        raise ValueError("text must not contain wallet send or signing keywords")


def _assert_readonly_sql(sql: str) -> None:
    if _READONLY_SQL_FORBIDDEN.search(sql):
        raise ValueError("SQL must be read-only SELECT only")


def _assert_insert_sql_targets_plan_tables_only(sql: str) -> None:
    assert_no_wallet_send_keywords(sql)
    lowered = sql.lower()
    if "insert into" not in lowered:
        raise ValueError("insert SQL must contain INSERT INTO")
    for token in re.findall(r"insert\s+into\s+([a-z0-9_]+)", lowered):
        if token not in _PLAN_INSERT_TABLES:
            raise ValueError(f"insert SQL must not target table: {token}")


def normalize_wallet_name(value: str) -> str:
    trimmed = str(value).strip()
    if not trimmed:
        raise ValueError("wallet_name is required")
    return trimmed


def parse_decimal_amount(value: str, *, field_name: str) -> Decimal:
    raw = str(value).strip()
    if not raw:
        raise ValueError(f"{field_name} is required")
    amount = Decimal(raw)
    if amount < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return amount


def parse_reserve_fraction(value: str | float | Decimal) -> Decimal:
    fraction = Decimal(str(value))
    if fraction < 0 or fraction > 1:
        raise ValueError("reserve_fraction must be between 0 and 1")
    return fraction


def compute_reserve_amount(
    trusted_balance_snapshot: Decimal,
    reserve_fraction: Decimal,
) -> Decimal:
    return _quantize_amount(trusted_balance_snapshot * reserve_fraction)


def compute_max_spendable_amount(
    trusted_balance_snapshot: Decimal,
    reserve_amount: Decimal,
) -> Decimal:
    return _quantize_amount(trusted_balance_snapshot - reserve_amount)


def build_credit_run_for_plan_sql() -> str:
    sql = """
SELECT
  id,
  wallet_name,
  maturity_status,
  status,
  reward_amount_total
FROM sc_node_reward_credit_runs
WHERE id = %(credit_run_id)s
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_credits_for_plan_sql() -> str:
    sql = """
SELECT
  c.id,
  c.credit_run_id,
  c.sc_node_id,
  n.display_name AS sc_node_display_name,
  c.credit_amount,
  c.credit_status
FROM sc_node_reward_credits c
LEFT JOIN sc_nodes n ON n.id = c.sc_node_id
WHERE c.credit_run_id = %(credit_run_id)s
  AND c.credit_status = 'draft'
ORDER BY c.sc_node_id, c.id
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_active_default_payout_addresses_sql() -> str:
    sql = """
SELECT
  a.sc_node_id,
  a.payout_address,
  n.display_name AS sc_node_display_name
FROM sc_node_payout_addresses a
LEFT JOIN sc_nodes n ON n.id = a.sc_node_id
WHERE a.sc_node_id = %(sc_node_id)s
  AND a.status = 'active'
  AND a.is_default = true
ORDER BY a.id
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_existing_draft_plan_sql() -> str:
    sql = """
SELECT id
FROM sc_node_payout_plans
WHERE credit_run_id = %(credit_run_id)s
  AND status = 'draft'
LIMIT 1
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_insert_payout_plan_sql() -> str:
    sql = """
INSERT INTO sc_node_payout_plans (
  credit_run_id,
  wallet_name,
  status,
  reserve_fraction,
  trusted_balance_snapshot,
  reserve_amount,
  max_spendable_amount,
  planned_amount_total,
  row_count,
  notes
) VALUES (
  %(credit_run_id)s,
  %(wallet_name)s,
  %(status)s,
  %(reserve_fraction)s,
  %(trusted_balance_snapshot)s,
  %(reserve_amount)s,
  %(max_spendable_amount)s,
  %(planned_amount_total)s,
  %(row_count)s,
  %(notes)s
)
RETURNING id
""".strip()
    _assert_insert_sql_targets_plan_tables_only(sql)
    return sql


def build_insert_payout_plan_row_sql() -> str:
    sql = """
INSERT INTO sc_node_payout_plan_rows (
  payout_plan_id,
  credit_id,
  sc_node_id,
  sc_node_display_name,
  payout_address,
  payout_amount,
  row_status
) VALUES (
  %(payout_plan_id)s,
  %(credit_id)s,
  %(sc_node_id)s,
  %(sc_node_display_name)s,
  %(payout_address)s,
  %(payout_amount)s,
  %(row_status)s
)
""".strip()
    _assert_insert_sql_targets_plan_tables_only(sql)
    return sql


def build_payout_plans_sql() -> str:
    sql = """
SELECT
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
  created_at,
  updated_at
FROM sc_node_payout_plans
ORDER BY created_at DESC, id DESC
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_payout_plan_details_sql(payout_plan_id: int) -> str:
    safe_id = int(payout_plan_id)
    sql = f"""
SELECT
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
  created_at,
  updated_at
FROM sc_node_payout_plans
WHERE id = {safe_id}
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_payout_plan_rows_sql(payout_plan_id: int) -> str:
    safe_id = int(payout_plan_id)
    sql = f"""
SELECT
  id,
  payout_plan_id,
  credit_id,
  sc_node_id,
  sc_node_display_name,
  payout_address,
  payout_amount,
  row_status,
  created_at,
  updated_at
FROM sc_node_payout_plan_rows
WHERE payout_plan_id = {safe_id}
ORDER BY payout_amount DESC, sc_node_id
""".strip()
    _assert_readonly_sql(sql)
    return sql


def resolve_active_default_payout_address(
    address_rows: list[Mapping[str, Any]],
    *,
    sc_node_id: str,
) -> tuple[str | None, str | None]:
    if not address_rows:
        return None, f"missing active/default payout address for {sc_node_id}"
    if len(address_rows) > 1:
        return None, f"duplicate active/default payout address for {sc_node_id}"
    address = str(address_rows[0].get("payout_address") or "").strip()
    if not address:
        return None, f"empty payout address for {sc_node_id}"
    return address, None


def evaluate_credit_run_refusal(credit_run: Mapping[str, Any] | None) -> str | None:
    if credit_run is None:
        return "credit run not found"
    if str(credit_run.get("status")) != REQUIRED_CREDIT_RUN_STATUS:
        return f"credit run status must be {REQUIRED_CREDIT_RUN_STATUS}"
    if str(credit_run.get("maturity_status")) != REQUIRED_MATURITY_STATUS:
        return f"credit run maturity_status must be {REQUIRED_MATURITY_STATUS}"
    return None


def evaluate_duplicate_draft_plan_refusal(
    existing_plan_id: int | None,
) -> str | None:
    if existing_plan_id is not None:
        return (
            f"draft payout plan already exists for credit_run_id "
            f"(plan id {existing_plan_id})"
        )
    return None


def evaluate_spend_cap_refusal(
    *,
    planned_amount_total: Decimal,
    max_spendable_amount: Decimal,
) -> str | None:
    if planned_amount_total > max_spendable_amount:
        return (
            "planned_amount_total exceeds max_spendable_amount "
            f"({planned_amount_total} > {max_spendable_amount})"
        )
    return None


def build_payout_plan_preview(
    *,
    credit_run_id: int,
    wallet_name: str,
    reserve_fraction: Decimal,
    trusted_balance_snapshot: Decimal,
    credit_run: Mapping[str, Any] | None,
    credits: list[Mapping[str, Any]],
    address_lookup: dict[str, list[Mapping[str, Any]]],
    existing_draft_plan_id: int | None = None,
) -> PayoutPlanPreview:
    refusal_parts: list[str] = []

    credit_refusal = evaluate_credit_run_refusal(credit_run)
    if credit_refusal:
        refusal_parts.append(credit_refusal)

    if credit_run is not None and str(credit_run.get("wallet_name")) != wallet_name:
        refusal_parts.append("wallet_name does not match credit run")

    duplicate_refusal = evaluate_duplicate_draft_plan_refusal(existing_draft_plan_id)
    if duplicate_refusal:
        refusal_parts.append(duplicate_refusal)

    if not credits:
        refusal_parts.append("credit run has no draft credits")

    reserve_amount = compute_reserve_amount(trusted_balance_snapshot, reserve_fraction)
    max_spendable_amount = compute_max_spendable_amount(
        trusted_balance_snapshot,
        reserve_amount,
    )

    rows: list[PayoutPlanRowPreview] = []
    for credit in credits:
        sc_node_id = str(credit["sc_node_id"])
        address_rows = address_lookup.get(sc_node_id, [])
        payout_address, address_refusal = resolve_active_default_payout_address(
            address_rows,
            sc_node_id=sc_node_id,
        )
        if address_refusal:
            refusal_parts.append(address_refusal)
            continue
        assert payout_address is not None
        rows.append(
            PayoutPlanRowPreview(
                credit_id=int(credit["id"]),
                sc_node_id=sc_node_id,
                sc_node_display_name=credit.get("sc_node_display_name"),
                payout_address=payout_address,
                payout_amount=_to_decimal(credit.get("credit_amount")),
            )
        )

    planned_amount_total = sum((row.payout_amount for row in rows), Decimal("0"))
    spend_refusal = evaluate_spend_cap_refusal(
        planned_amount_total=planned_amount_total,
        max_spendable_amount=max_spendable_amount,
    )
    if spend_refusal:
        refusal_parts.append(spend_refusal)

    refusal_reason = "; ".join(refusal_parts) if refusal_parts else None
    return PayoutPlanPreview(
        credit_run_id=credit_run_id,
        wallet_name=wallet_name,
        reserve_fraction=reserve_fraction,
        trusted_balance_snapshot=trusted_balance_snapshot,
        reserve_amount=reserve_amount,
        max_spendable_amount=max_spendable_amount,
        planned_amount_total=planned_amount_total,
        row_count=len(rows),
        rows=tuple(rows),
        plan_allowed=refusal_reason is None,
        refusal_reason=refusal_reason,
    )


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


def _quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000000000001"), rounding=ROUND_DOWN)


def reserve_fraction_to_percent_string(reserve_fraction: Decimal) -> str:
    percent = reserve_fraction * Decimal("100")
    return _serialize_numeric(percent.quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def payout_plan_preview_to_dict(preview: PayoutPlanPreview) -> dict[str, Any]:
    return {
        "credit_run_id": preview.credit_run_id,
        "wallet_name": preview.wallet_name,
        "reserve_fraction": _serialize_numeric(preview.reserve_fraction),
        "reserve_percent": reserve_fraction_to_percent_string(preview.reserve_fraction),
        "trusted_balance_snapshot": _serialize_numeric(preview.trusted_balance_snapshot),
        "reserve_amount": _serialize_numeric(preview.reserve_amount),
        "max_spendable_amount": _serialize_numeric(preview.max_spendable_amount),
        "planned_amount_total": _serialize_numeric(preview.planned_amount_total),
        "row_count": preview.row_count,
        "rows": [
            {
                "credit_id": row.credit_id,
                "sc_node_id": row.sc_node_id,
                "sc_node_display_name": row.sc_node_display_name,
                "payout_address": row.payout_address,
                "payout_amount": _serialize_numeric(row.payout_amount),
            }
            for row in preview.rows
        ],
        "plan_allowed": preview.plan_allowed,
        "refusal_reason": preview.refusal_reason,
        "accounting_note": (
            "payout plan is a no-send proposal only; not a wallet transaction"
        ),
    }


def row_to_payout_plan_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    reserve_fraction = _to_decimal(row.get("reserve_fraction"))
    return {
        "id": _to_int(row["id"]),
        "credit_run_id": _to_int(row.get("credit_run_id")),
        "wallet_name": row.get("wallet_name"),
        "status": row.get("status"),
        "reserve_fraction": _serialize_numeric(reserve_fraction),
        "reserve_percent": reserve_fraction_to_percent_string(reserve_fraction),
        "trusted_balance_snapshot": _serialize_numeric(
            _to_decimal(row.get("trusted_balance_snapshot"))
        ),
        "reserve_amount": _serialize_numeric(_to_decimal(row.get("reserve_amount"))),
        "max_spendable_amount": _serialize_numeric(
            _to_decimal(row.get("max_spendable_amount"))
        ),
        "planned_amount_total": _serialize_numeric(
            _to_decimal(row.get("planned_amount_total"))
        ),
        "row_count": _to_int(row.get("row_count")),
        "notes": row.get("notes"),
        "created_at": _serialize_datetime(row.get("created_at")),
        "updated_at": _serialize_datetime(row.get("updated_at")),
    }


def row_to_payout_plan_row_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _to_int(row["id"]),
        "payout_plan_id": _to_int(row.get("payout_plan_id")),
        "credit_id": _to_int(row.get("credit_id")),
        "sc_node_id": str(row["sc_node_id"]),
        "sc_node_display_name": row.get("sc_node_display_name"),
        "payout_address": str(row["payout_address"]),
        "payout_amount": _serialize_numeric(_to_decimal(row.get("payout_amount"))),
        "row_status": row.get("row_status"),
        "created_at": _serialize_datetime(row.get("created_at")),
        "updated_at": _serialize_datetime(row.get("updated_at")),
    }


def _serialize_datetime(value: object) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[union-attr]
    return str(value)
