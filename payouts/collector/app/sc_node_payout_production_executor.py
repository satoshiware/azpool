from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any, Mapping

from payouts.collector.app import sc_node_payout_plan_review as plan_review
from payouts.collector.app import sc_node_payout_planner as planner
from payouts.collector.app import sc_node_payout_production_preflight as production_preflight

DEFAULT_RESERVE_PERCENT = Decimal("0.500000")

EXECUTION_STATUS_DRAFT = "draft"
EXECUTION_STATUS_SENT = "sent"
EXECUTION_STATUS_CONFIRMED = "confirmed"
EXECUTION_STATUS_REFUSED = "refused"
EXECUTION_STATUS_VOID = "void"

ROW_STATUS_DRAFT = "draft"
ROW_STATUS_SENT = "sent"
ROW_STATUS_CONFIRMED = "confirmed"
ROW_STATUS_REFUSED = "refused"
ROW_STATUS_VOID = "void"

ACTIVE_EXECUTION_STATUSES = frozenset(
    {EXECUTION_STATUS_SENT, EXECUTION_STATUS_CONFIRMED}
)

_PRODUCTION_EXECUTION_MUTATION_TABLES = frozenset(
    {
        "sc_node_payout_production_executions",
        "sc_node_payout_production_execution_rows",
    }
)

_FORBIDDEN_WALLET_RPC_KEYWORDS = re.compile(
    r"\b("
    r"sendmany|sendrawtransaction|walletpassphrase|"
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


@dataclass(frozen=True)
class WalletBalance:
    trusted: Decimal
    immature: Decimal


@dataclass(frozen=True)
class ProductionExecutionRow:
    payout_plan_row_id: int
    sc_node_id: str
    payout_address: str
    payout_amount: Decimal
    registry_payout_address: str | None
    row_status: str
    refusal_reason: str | None


@dataclass(frozen=True)
class ProductionExecutionPreview:
    payout_plan_id: int
    production_preflight_id: int
    source_wallet_name: str
    planned_amount_total: Decimal
    row_count: int
    wallet_balance: WalletBalance
    reserve_amount: Decimal
    spendable_after_reserve: Decimal
    expected_confirmation_phrase: str
    execution_allowed: bool
    refusal_reason: str | None
    rows: tuple[ProductionExecutionRow, ...]


@dataclass(frozen=True)
class RealSendResult:
    payout_plan_row_id: int
    payout_address: str
    payout_amount: Decimal
    txid: str | None
    sent: bool
    refusal_reason: str | None


def assert_no_forbidden_wallet_rpc_keywords_except_sendtoaddress(text: str) -> None:
    if _FORBIDDEN_WALLET_RPC_KEYWORDS.search(text):
        raise ValueError("text must not contain forbidden wallet RPC keywords")


def _assert_readonly_sql(sql: str) -> None:
    if _READONLY_SQL_FORBIDDEN.search(sql):
        raise ValueError("SQL must be read-only SELECT only")


def _assert_production_execution_mutation_sql(sql: str) -> None:
    assert_no_forbidden_wallet_rpc_keywords_except_sendtoaddress(sql)
    lowered = sql.lower()
    if "insert into" not in lowered and "update" not in lowered:
        raise ValueError("production execution SQL must INSERT or UPDATE")
    for token in re.findall(r"\b(?:insert\s+into|update)\s+([a-z0-9_]+)\b", lowered):
        if token not in _PRODUCTION_EXECUTION_MUTATION_TABLES:
            raise ValueError(f"production execution SQL must not target table: {token}")


def _quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000000000001"), rounding=ROUND_DOWN)


def parse_wallet_balance_from_getbalances(payload: Mapping[str, Any]) -> WalletBalance:
    return production_preflight.parse_wallet_balance_from_getbalances(payload)


def calculate_execution_guardrails(
    *,
    trusted_balance: Decimal,
    planned_amount_total: Decimal,
    reserve_percent: Decimal = DEFAULT_RESERVE_PERCENT,
) -> dict[str, Decimal]:
    percent = Decimal(str(reserve_percent))
    if percent < 0 or percent > 1:
        raise ValueError("reserve_percent must be between 0 and 1")
    reserve_amount = _quantize_amount(trusted_balance * percent)
    spendable_after_reserve = _quantize_amount(trusted_balance - reserve_amount)
    if spendable_after_reserve < 0:
        spendable_after_reserve = Decimal("0")
    return {
        "reserve_percent": percent,
        "reserve_amount": reserve_amount,
        "spendable_after_reserve": spendable_after_reserve,
        "planned_amount_total": _quantize_amount(planned_amount_total),
        "trusted_balance": _quantize_amount(trusted_balance),
    }


def build_expected_confirmation_phrase(
    payout_plan_id: int,
    planned_amount_total: Decimal,
    source_wallet_name: str,
) -> str:
    amount = planner._serialize_numeric(_quantize_amount(planned_amount_total))
    wallet = str(source_wallet_name).strip()
    if not wallet:
        raise ValueError("source_wallet_name is required")
    return f"SEND {amount} FROM {wallet} FOR PLAN {int(payout_plan_id)}"


def verify_confirmation_phrase(
    *,
    confirmation_phrase: str,
    payout_plan_id: int,
    planned_amount_total: Decimal,
    source_wallet_name: str,
) -> bool:
    expected = build_expected_confirmation_phrase(
        payout_plan_id,
        planned_amount_total,
        source_wallet_name,
    )
    return confirmation_phrase.strip() == expected


def normalize_source_wallet_name(value: str) -> str:
    return production_preflight.normalize_source_wallet_name(value)


def normalize_idempotency_key(value: str) -> str:
    key = str(value).strip()
    if not key:
        raise ValueError("idempotency_key is required")
    return key


def normalize_confirmation_phrase(value: str) -> str:
    phrase = str(value).strip()
    if not phrase:
        raise ValueError("confirm_phrase is required")
    return phrase


def build_passed_production_preflight_sql() -> str:
    sql = """
SELECT
  id,
  payout_plan_id,
  source_wallet_name,
  preflight_status,
  execution_allowed,
  refusal_reason,
  trusted_balance,
  immature_balance,
  planned_amount_total,
  reserve_mode,
  reserve_percent,
  reserve_amount,
  spendable_after_reserve,
  max_spend_percent,
  operator_override,
  wallet_balance_source,
  idempotency_key,
  notes,
  created_at,
  updated_at
FROM sc_node_payout_production_preflights
WHERE id = %(production_preflight_id)s
  AND payout_plan_id = %(payout_plan_id)s
  AND preflight_status = 'passed'
  AND execution_allowed = true
LIMIT 1
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_production_preflight_rows_for_execution_sql() -> str:
    sql = """
SELECT
  id,
  production_preflight_id,
  payout_plan_row_id,
  sc_node_id,
  payout_address,
  payout_amount,
  row_status,
  refusal_reason,
  created_at,
  updated_at
FROM sc_node_payout_production_preflight_rows
WHERE production_preflight_id = %(production_preflight_id)s
ORDER BY payout_plan_row_id
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_approved_payout_plan_for_execution_sql(payout_plan_id: int) -> str:
    return production_preflight.build_approved_payout_plan_sql(payout_plan_id)


def build_approved_payout_plan_rows_for_execution_sql(payout_plan_id: int) -> str:
    return production_preflight.build_approved_payout_plan_rows_sql(payout_plan_id)


def build_execution_by_plan_idempotency_sql() -> str:
    sql = """
SELECT
  id,
  payout_plan_id,
  production_preflight_id,
  source_wallet_name,
  status,
  planned_amount_total,
  trusted_balance_before,
  immature_balance_before,
  reserve_amount,
  spendable_after_reserve,
  execution_attempt_count,
  idempotency_key,
  confirmation_phrase,
  txid,
  refusal_reason,
  notes,
  created_at,
  updated_at
FROM sc_node_payout_production_executions
WHERE payout_plan_id = %(payout_plan_id)s
  AND idempotency_key = %(idempotency_key)s
LIMIT 1
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_existing_active_production_execution_sql() -> str:
    sql = """
SELECT
  id,
  payout_plan_id,
  production_preflight_id,
  source_wallet_name,
  status,
  planned_amount_total,
  trusted_balance_before,
  immature_balance_before,
  reserve_amount,
  spendable_after_reserve,
  execution_attempt_count,
  idempotency_key,
  confirmation_phrase,
  txid,
  refusal_reason,
  notes,
  created_at,
  updated_at
FROM sc_node_payout_production_executions
WHERE payout_plan_id = %(payout_plan_id)s
  AND status IN ('sent', 'confirmed')
ORDER BY id DESC
LIMIT 1
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_insert_production_execution_sql() -> str:
    sql = """
INSERT INTO sc_node_payout_production_executions (
  payout_plan_id,
  production_preflight_id,
  source_wallet_name,
  status,
  planned_amount_total,
  trusted_balance_before,
  immature_balance_before,
  reserve_amount,
  spendable_after_reserve,
  execution_attempt_count,
  idempotency_key,
  confirmation_phrase,
  txid,
  refusal_reason,
  notes
) VALUES (
  %(payout_plan_id)s,
  %(production_preflight_id)s,
  %(source_wallet_name)s,
  %(status)s,
  %(planned_amount_total)s,
  %(trusted_balance_before)s,
  %(immature_balance_before)s,
  %(reserve_amount)s,
  %(spendable_after_reserve)s,
  %(execution_attempt_count)s,
  %(idempotency_key)s,
  %(confirmation_phrase)s,
  %(txid)s,
  %(refusal_reason)s,
  %(notes)s
)
RETURNING id
""".strip()
    _assert_production_execution_mutation_sql(sql)
    return sql


def build_insert_production_execution_row_sql() -> str:
    sql = """
INSERT INTO sc_node_payout_production_execution_rows (
  production_execution_id,
  payout_plan_row_id,
  sc_node_id,
  payout_address,
  payout_amount,
  row_status,
  txid,
  refusal_reason
) VALUES (
  %(production_execution_id)s,
  %(payout_plan_row_id)s,
  %(sc_node_id)s,
  %(payout_address)s,
  %(payout_amount)s,
  %(row_status)s,
  %(txid)s,
  %(refusal_reason)s
)
RETURNING id
""".strip()
    _assert_production_execution_mutation_sql(sql)
    return sql


def build_mark_production_execution_sent_sql() -> str:
    sql = """
UPDATE sc_node_payout_production_executions
SET status = 'sent',
    txid = %(txid)s,
    execution_attempt_count = execution_attempt_count + 1,
    updated_at = now()
WHERE id = %(production_execution_id)s
  AND status = 'draft'
RETURNING id
""".strip()
    _assert_production_execution_mutation_sql(sql)
    return sql


def build_mark_production_execution_refused_sql() -> str:
    sql = """
UPDATE sc_node_payout_production_executions
SET status = 'refused',
    refusal_reason = %(refusal_reason)s,
    updated_at = now()
WHERE id = %(production_execution_id)s
RETURNING id
""".strip()
    _assert_production_execution_mutation_sql(sql)
    return sql


def build_mark_production_execution_row_sent_sql() -> str:
    sql = """
UPDATE sc_node_payout_production_execution_rows
SET row_status = 'sent',
    txid = %(txid)s,
    updated_at = now()
WHERE id = %(production_execution_row_id)s
  AND row_status = 'draft'
RETURNING id
""".strip()
    _assert_production_execution_mutation_sql(sql)
    return sql


def build_mark_production_execution_row_refused_sql() -> str:
    sql = """
UPDATE sc_node_payout_production_execution_rows
SET row_status = 'refused',
    refusal_reason = %(refusal_reason)s,
    updated_at = now()
WHERE id = %(production_execution_row_id)s
RETURNING id
""".strip()
    _assert_production_execution_mutation_sql(sql)
    return sql


def build_mark_production_execution_confirmed_sql() -> str:
    sql = """
UPDATE sc_node_payout_production_executions
SET status = 'confirmed',
    updated_at = now()
WHERE id = %(production_execution_id)s
  AND status = 'sent'
RETURNING id
""".strip()
    _assert_production_execution_mutation_sql(sql)
    return sql


def build_mark_production_execution_rows_confirmed_sql() -> str:
    sql = """
UPDATE sc_node_payout_production_execution_rows
SET row_status = 'confirmed',
    updated_at = now()
WHERE production_execution_id = %(production_execution_id)s
  AND row_status = 'sent'
""".strip()
    _assert_production_execution_mutation_sql(sql)
    return sql


def build_production_executions_sql() -> str:
    sql = """
SELECT
  id,
  payout_plan_id,
  production_preflight_id,
  source_wallet_name,
  status,
  planned_amount_total,
  trusted_balance_before,
  immature_balance_before,
  reserve_amount,
  spendable_after_reserve,
  execution_attempt_count,
  idempotency_key,
  confirmation_phrase,
  txid,
  refusal_reason,
  notes,
  created_at,
  updated_at
FROM sc_node_payout_production_executions
ORDER BY created_at DESC, id DESC
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_production_execution_details_sql(production_execution_id: int) -> str:
    safe_id = int(production_execution_id)
    sql = f"""
SELECT
  id,
  payout_plan_id,
  production_preflight_id,
  source_wallet_name,
  status,
  planned_amount_total,
  trusted_balance_before,
  immature_balance_before,
  reserve_amount,
  spendable_after_reserve,
  execution_attempt_count,
  idempotency_key,
  confirmation_phrase,
  txid,
  refusal_reason,
  notes,
  created_at,
  updated_at
FROM sc_node_payout_production_executions
WHERE id = {safe_id}
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_production_execution_rows_sql(production_execution_id: int) -> str:
    safe_id = int(production_execution_id)
    sql = f"""
SELECT
  id,
  production_execution_id,
  payout_plan_row_id,
  sc_node_id,
  payout_address,
  payout_amount,
  row_status,
  txid,
  refusal_reason,
  created_at,
  updated_at
FROM sc_node_payout_production_execution_rows
WHERE production_execution_id = {safe_id}
ORDER BY payout_amount DESC, sc_node_id
""".strip()
    _assert_readonly_sql(sql)
    return sql


def _evaluate_row_registry(
    *,
    plan_row: Mapping[str, Any],
    address_lookup: dict[str, list[Mapping[str, Any]]],
) -> tuple[str, str | None, str | None]:
    sc_node_id = str(plan_row["sc_node_id"])
    frozen = str(plan_row["payout_address"])
    address_rows = address_lookup.get(sc_node_id, [])
    registry_address, lookup_refusal = planner.resolve_active_default_payout_address(
        address_rows,
        sc_node_id=sc_node_id,
    )
    if lookup_refusal:
        return ROW_STATUS_REFUSED, None, lookup_refusal
    drift = plan_review.evaluate_address_drift(
        sc_node_id=sc_node_id,
        frozen_address=frozen,
        registry_address=registry_address,
    )
    if drift:
        return ROW_STATUS_REFUSED, registry_address, drift
    return ROW_STATUS_DRAFT, registry_address, None


def _preflight_rows_by_plan_row_id(
    preflight_rows: list[Mapping[str, Any]],
) -> dict[int, Mapping[str, Any]]:
    return {planner._to_int(row["payout_plan_row_id"]): row for row in preflight_rows}


def _validate_preflight_row_alignment(
    *,
    plan_rows: list[Mapping[str, Any]],
    preflight_rows: list[Mapping[str, Any]],
) -> str | None:
    if len(plan_rows) != len(preflight_rows):
        return (
            f"preflight row count {len(preflight_rows)} does not match "
            f"approved plan row count {len(plan_rows)}"
        )
    preflight_by_row = _preflight_rows_by_plan_row_id(preflight_rows)
    plan_row_ids = {planner._to_int(row["id"]) for row in plan_rows}
    if set(preflight_by_row.keys()) != plan_row_ids:
        return "preflight rows do not match approved payout plan row ids"
    for row in plan_rows:
        row_id = planner._to_int(row["id"])
        preflight_row = preflight_by_row[row_id]
        if str(preflight_row.get("row_status")) != production_preflight.ROW_STATUS_CHECKED:
            return f"preflight row {row_id} is not checked"
        plan_amount = _quantize_amount(planner._to_decimal(row.get("payout_amount")))
        preflight_amount = _quantize_amount(
            planner._to_decimal(preflight_row.get("payout_amount"))
        )
        if plan_amount != preflight_amount:
            return f"amount mismatch for payout plan row {row_id}"
        if str(row.get("payout_address")) != str(preflight_row.get("payout_address")):
            return f"address mismatch for payout plan row {row_id}"
        if str(row.get("sc_node_id")) != str(preflight_row.get("sc_node_id")):
            return f"sc_node_id mismatch for payout plan row {row_id}"
    return None


def evaluate_balance_refusal(
    *,
    wallet_balance: WalletBalance,
    planned_amount_total: Decimal,
    reserve_percent: Decimal = DEFAULT_RESERVE_PERCENT,
) -> str | None:
    guardrails = calculate_execution_guardrails(
        trusted_balance=wallet_balance.trusted,
        planned_amount_total=planned_amount_total,
        reserve_percent=reserve_percent,
    )
    if planned_amount_total <= 0:
        return "planned_amount_total must be greater than zero"
    if wallet_balance.trusted <= 0:
        return "trusted wallet balance must be greater than zero"
    if planned_amount_total > wallet_balance.trusted:
        return (
            "planned_amount_total exceeds current trusted wallet balance "
            f"({planner._serialize_numeric(wallet_balance.trusted)})"
        )
    spendable = guardrails["spendable_after_reserve"]
    if planned_amount_total > spendable:
        return (
            "planned_amount_total exceeds spendable_after_reserve "
            f"({planner._serialize_numeric(spendable)})"
        )
    return None


def evaluate_preview_refusal(
    *,
    plan: Mapping[str, Any] | None,
    plan_rows: list[Mapping[str, Any]],
    preflight: Mapping[str, Any] | None,
    preflight_rows: list[Mapping[str, Any]],
    source_wallet_name: str,
    wallet_balance: WalletBalance,
    address_lookup: dict[str, list[Mapping[str, Any]]],
) -> str | None:
    if preflight is None:
        return "passed production preflight not found"
    if plan is None:
        return "approved payout plan not found"
    if str(plan.get("status")) != plan_review.PLAN_STATUS_APPROVED:
        return "payout plan status must be approved"
    if not plan_rows:
        return "payout plan has no approved rows"
    for row in plan_rows:
        if str(row.get("row_status")) != plan_review.ROW_STATUS_APPROVED:
            return "all payout plan rows must be approved"
    preflight_wallet = str(preflight.get("source_wallet_name") or "").strip()
    if preflight_wallet != source_wallet_name:
        return "source_wallet_name does not match production preflight"
    alignment = _validate_preflight_row_alignment(
        plan_rows=plan_rows,
        preflight_rows=preflight_rows,
    )
    if alignment:
        return alignment
    planned_total = planner._to_decimal(plan.get("planned_amount_total"))
    preflight_planned = planner._to_decimal(preflight.get("planned_amount_total"))
    if planned_total != preflight_planned:
        return "planned_amount_total does not match production preflight"
    row_refusals: list[str] = []
    for row in plan_rows:
        _, _, row_refusal = _evaluate_row_registry(
            plan_row=row,
            address_lookup=address_lookup,
        )
        if row_refusal:
            row_refusals.append(row_refusal)
    if row_refusals:
        return "; ".join(row_refusals)
    balance_refusal = evaluate_balance_refusal(
        wallet_balance=wallet_balance,
        planned_amount_total=planned_total,
    )
    if balance_refusal:
        return balance_refusal
    return None


def evaluate_execute_real_refusal(
    *,
    plan: Mapping[str, Any] | None,
    plan_rows: list[Mapping[str, Any]],
    preflight: Mapping[str, Any] | None,
    preflight_rows: list[Mapping[str, Any]],
    source_wallet_name: str,
    wallet_balance: WalletBalance,
    address_lookup: dict[str, list[Mapping[str, Any]]],
    confirmation_phrase: str,
    existing_by_key: Mapping[str, Any] | None,
    active_execution: Mapping[str, Any] | None,
    idempotency_key: str,
    allow_multiple_rows: bool = False,
) -> str | None:
    if existing_by_key is not None:
        return None
    preview_refusal = evaluate_preview_refusal(
        plan=plan,
        plan_rows=plan_rows,
        preflight=preflight,
        preflight_rows=preflight_rows,
        source_wallet_name=source_wallet_name,
        wallet_balance=wallet_balance,
        address_lookup=address_lookup,
    )
    if preview_refusal:
        return preview_refusal
    if len(plan_rows) > 1 and not allow_multiple_rows:
        return (
            "execute-real refuses multiple payout rows without --allow-multiple-rows"
        )
    assert plan is not None
    planned_total = planner._to_decimal(plan.get("planned_amount_total"))
    if not verify_confirmation_phrase(
        confirmation_phrase=confirmation_phrase,
        payout_plan_id=planner._to_int(plan.get("id")),
        planned_amount_total=planned_total,
        source_wallet_name=source_wallet_name,
    ):
        return "confirmation phrase mismatch"
    if active_execution is not None:
        active_key = str(active_execution.get("idempotency_key"))
        if active_key != idempotency_key:
            return (
                "active production execution already exists for payout_plan_id "
                f"(execution id {active_execution.get('id')}, "
                f"idempotency_key {active_key})"
            )
    return None


def evaluate_mark_confirmed_refusal(
    execution: Mapping[str, Any] | None,
) -> str | None:
    if execution is None:
        return "production execution not found"
    status = str(execution.get("status"))
    if status == EXECUTION_STATUS_REFUSED:
        return "cannot confirm refused production execution"
    if status == EXECUTION_STATUS_CONFIRMED:
        return None
    if status != EXECUTION_STATUS_SENT:
        return "production execution status must be sent to confirm"
    return None


def build_production_execution_preview(
    *,
    payout_plan_id: int,
    production_preflight_id: int,
    source_wallet_name: str,
    plan: Mapping[str, Any] | None,
    plan_rows: list[Mapping[str, Any]],
    preflight: Mapping[str, Any] | None,
    preflight_rows: list[Mapping[str, Any]],
    wallet_balance: WalletBalance,
    address_lookup: dict[str, list[Mapping[str, Any]]],
) -> ProductionExecutionPreview:
    planned_total = (
        planner._to_decimal(plan.get("planned_amount_total"))
        if plan is not None
        else Decimal("0")
    )
    guardrails = calculate_execution_guardrails(
        trusted_balance=wallet_balance.trusted,
        planned_amount_total=planned_total,
    )
    expected_phrase = build_expected_confirmation_phrase(
        payout_plan_id,
        planned_total,
        source_wallet_name,
    )
    preview_rows: list[ProductionExecutionRow] = []
    for row in plan_rows:
        row_status, registry_address, row_refusal = _evaluate_row_registry(
            plan_row=row,
            address_lookup=address_lookup,
        )
        preview_rows.append(
            ProductionExecutionRow(
                payout_plan_row_id=planner._to_int(row["id"]),
                sc_node_id=str(row["sc_node_id"]),
                payout_address=str(row["payout_address"]),
                payout_amount=planner._to_decimal(row.get("payout_amount")),
                registry_payout_address=registry_address,
                row_status=row_status,
                refusal_reason=row_refusal,
            )
        )
    refusal = evaluate_preview_refusal(
        plan=plan,
        plan_rows=plan_rows,
        preflight=preflight,
        preflight_rows=preflight_rows,
        source_wallet_name=source_wallet_name,
        wallet_balance=wallet_balance,
        address_lookup=address_lookup,
    )
    return ProductionExecutionPreview(
        payout_plan_id=payout_plan_id,
        production_preflight_id=production_preflight_id,
        source_wallet_name=source_wallet_name,
        planned_amount_total=planned_total,
        row_count=len(preview_rows),
        wallet_balance=wallet_balance,
        reserve_amount=guardrails["reserve_amount"],
        spendable_after_reserve=guardrails["spendable_after_reserve"],
        expected_confirmation_phrase=expected_phrase,
        execution_allowed=refusal is None,
        refusal_reason=refusal,
        rows=tuple(preview_rows),
    )


def production_execution_preview_to_dict(
    preview: ProductionExecutionPreview,
) -> dict[str, Any]:
    return {
        "payout_plan_id": preview.payout_plan_id,
        "production_preflight_id": preview.production_preflight_id,
        "source_wallet_name": preview.source_wallet_name,
        "planned_amount_total": planner._serialize_numeric(preview.planned_amount_total),
        "row_count": preview.row_count,
        "trusted_balance": planner._serialize_numeric(preview.wallet_balance.trusted),
        "immature_balance": planner._serialize_numeric(preview.wallet_balance.immature),
        "reserve_amount": planner._serialize_numeric(preview.reserve_amount),
        "spendable_after_reserve": planner._serialize_numeric(
            preview.spendable_after_reserve
        ),
        "expected_confirmation_phrase": preview.expected_confirmation_phrase,
        "execution_allowed": preview.execution_allowed,
        "refusal_reason": preview.refusal_reason,
        "rows": [
            {
                "payout_plan_row_id": row.payout_plan_row_id,
                "sc_node_id": row.sc_node_id,
                "payout_address": row.payout_address,
                "registry_payout_address": row.registry_payout_address,
                "payout_amount": planner._serialize_numeric(row.payout_amount),
                "row_status": row.row_status,
                "refusal_reason": row.refusal_reason,
            }
            for row in preview.rows
        ],
        "accounting_note": (
            "preview only; execute-real requires exact confirmation phrase and fresh balance"
        ),
    }


def row_to_production_execution_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": planner._to_int(row["id"]),
        "payout_plan_id": planner._to_int(row.get("payout_plan_id")),
        "production_preflight_id": planner._to_int(row.get("production_preflight_id")),
        "source_wallet_name": row.get("source_wallet_name"),
        "status": row.get("status"),
        "planned_amount_total": planner._serialize_numeric(
            planner._to_decimal(row.get("planned_amount_total"))
        ),
        "trusted_balance_before": planner._serialize_numeric(
            planner._to_decimal(row.get("trusted_balance_before"))
        ),
        "immature_balance_before": planner._serialize_numeric(
            planner._to_decimal(row.get("immature_balance_before"))
        ),
        "reserve_amount": planner._serialize_numeric(
            planner._to_decimal(row.get("reserve_amount"))
        ),
        "spendable_after_reserve": planner._serialize_numeric(
            planner._to_decimal(row.get("spendable_after_reserve"))
        ),
        "execution_attempt_count": planner._to_int(row.get("execution_attempt_count")),
        "idempotency_key": row.get("idempotency_key"),
        "confirmation_phrase": row.get("confirmation_phrase"),
        "txid": row.get("txid"),
        "refusal_reason": row.get("refusal_reason"),
        "notes": row.get("notes"),
        "created_at": planner._serialize_datetime(row.get("created_at")),
        "updated_at": planner._serialize_datetime(row.get("updated_at")),
    }


def row_to_production_execution_row_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": planner._to_int(row["id"]),
        "production_execution_id": planner._to_int(row.get("production_execution_id")),
        "payout_plan_row_id": planner._to_int(row.get("payout_plan_row_id")),
        "sc_node_id": str(row["sc_node_id"]),
        "payout_address": str(row["payout_address"]),
        "payout_amount": planner._serialize_numeric(
            planner._to_decimal(row.get("payout_amount"))
        ),
        "row_status": row.get("row_status"),
        "txid": row.get("txid"),
        "refusal_reason": row.get("refusal_reason"),
        "created_at": planner._serialize_datetime(row.get("created_at")),
        "updated_at": planner._serialize_datetime(row.get("updated_at")),
    }


def build_sendtoaddress_argv(
    *,
    azc_bin: str,
    source_wallet_name: str,
    payout_address: str,
    payout_amount: Decimal,
) -> list[str]:
    assert_no_forbidden_wallet_rpc_keywords_except_sendtoaddress(azc_bin)
    amount = planner._serialize_numeric(_quantize_amount(payout_amount))
    address = str(payout_address).strip()
    if not address:
        raise ValueError("payout_address is required")
    argv = [
        azc_bin,
        f"-rpcwallet={source_wallet_name}",
        "sendtoaddress",
        address,
        amount,
    ]
    for arg in argv:
        assert_no_forbidden_wallet_rpc_keywords_except_sendtoaddress(arg)
    return argv
