from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from payouts.collector.app import sc_node_payout_plan_review as plan_review
from payouts.collector.app import sc_node_payout_planner as planner

EXECUTION_MODE_FAKE_REGTEST = "fake_regtest"
EXECUTION_MODE_REGTEST = "regtest"
VALID_EXECUTION_MODES = frozenset({EXECUTION_MODE_FAKE_REGTEST, EXECUTION_MODE_REGTEST})

EXECUTION_STATUS_DRAFT = "draft"
EXECUTION_STATUS_EXECUTING = "executing"
EXECUTION_STATUS_SENT = "sent"
EXECUTION_STATUS_CONFIRMED = "confirmed"
EXECUTION_STATUS_FAILED = "failed"

ROW_STATUS_PENDING = "pending"
ROW_STATUS_SENT = "sent"
ROW_STATUS_CONFIRMED = "confirmed"
ROW_STATUS_FAILED = "failed"

ACTIVE_EXECUTION_STATUSES = frozenset(
    {EXECUTION_STATUS_EXECUTING, EXECUTION_STATUS_SENT, EXECUTION_STATUS_CONFIRMED}
)

PRODUCTION_WALLET_BLOCKLIST = frozenset(
    {"wallet", "support", "support-wallet", "main", "production"}
)

_TEST_INSERT_TABLES = frozenset(
    {"sc_node_payout_test_executions", "sc_node_payout_test_execution_rows"}
)

_WALLET_SEND_KEYWORDS = re.compile(
    r"\b("
    r"sendmany|sendtoaddress|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|privkey|azc|azcoin-cli|subprocess"
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
class TestExecutionPreview:
    payout_plan_id: int
    mode: str
    test_wallet_name: str
    planned_amount_total: Decimal
    row_count: int
    rows: tuple[dict[str, Any], ...]
    execution_allowed: bool
    refusal_reason: str | None


def assert_no_wallet_send_keywords(text: str) -> None:
    if _WALLET_SEND_KEYWORDS.search(text):
        raise ValueError("text must not contain wallet RPC or send keywords")


def _assert_readonly_sql(sql: str) -> None:
    if _READONLY_SQL_FORBIDDEN.search(sql):
        raise ValueError("SQL must be read-only SELECT only")


def _assert_test_insert_sql(sql: str) -> None:
    assert_no_wallet_send_keywords(sql)
    lowered = sql.lower()
    if "insert into" not in lowered and "update" not in lowered:
        raise ValueError("test execution SQL must INSERT or UPDATE")
    for token in re.findall(r"\b(?:insert\s+into|update)\s+([a-z0-9_]+)\b", lowered):
        if token not in _TEST_INSERT_TABLES:
            raise ValueError(f"test execution SQL must not target table: {token}")


def normalize_execution_mode(value: str) -> str:
    mode = str(value).strip().lower()
    if mode not in VALID_EXECUTION_MODES:
        raise ValueError(f"mode must be one of: {', '.join(sorted(VALID_EXECUTION_MODES))}")
    return mode


def normalize_test_wallet_name(value: str) -> str:
    name = str(value).strip()
    if not name:
        raise ValueError("test_wallet_name is required")
    lowered = name.lower()
    if lowered in PRODUCTION_WALLET_BLOCKLIST:
        raise ValueError("test_wallet_name must not be a production wallet name")
    if not (lowered.startswith("fake-") or "regtest" in lowered):
        raise ValueError(
            "test_wallet_name must be test-only (fake- prefix or regtest in name)"
        )
    return name


def normalize_idempotency_key(value: str) -> str:
    key = str(value).strip()
    if not key:
        raise ValueError("idempotency_key is required")
    return key


def is_production_wallet_name(wallet_name: str) -> bool:
    return str(wallet_name).strip().lower() in PRODUCTION_WALLET_BLOCKLIST


def generate_fake_txid(
    *,
    payout_plan_id: int,
    idempotency_key: str,
    payout_plan_row_ids: list[int],
) -> str:
    payload = (
        f"fake_regtest:{payout_plan_id}:{idempotency_key}:"
        f"{','.join(str(i) for i in sorted(payout_plan_row_ids))}"
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"fake-regtest-{payout_plan_id}-{digest}"


def build_payout_plan_for_test_sql(payout_plan_id: int) -> str:
    return plan_review.build_payout_plan_for_review_sql(payout_plan_id)


def build_payout_plan_rows_for_test_sql(payout_plan_id: int) -> str:
    return plan_review.build_payout_plan_rows_for_review_sql(payout_plan_id)


def build_execution_by_plan_idempotency_sql() -> str:
    sql = """
SELECT
  id,
  payout_plan_id,
  mode,
  status,
  planned_amount_total,
  test_wallet_name,
  txid,
  execution_attempt_count,
  idempotency_key,
  notes,
  created_at,
  updated_at
FROM sc_node_payout_test_executions
WHERE payout_plan_id = %(payout_plan_id)s
  AND idempotency_key = %(idempotency_key)s
LIMIT 1
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_active_execution_for_plan_sql() -> str:
    sql = """
SELECT
  id,
  payout_plan_id,
  mode,
  status,
  planned_amount_total,
  test_wallet_name,
  txid,
  execution_attempt_count,
  idempotency_key,
  notes,
  created_at,
  updated_at
FROM sc_node_payout_test_executions
WHERE payout_plan_id = %(payout_plan_id)s
  AND status IN ('executing', 'sent', 'confirmed')
ORDER BY id DESC
LIMIT 1
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_insert_test_execution_sql() -> str:
    sql = """
INSERT INTO sc_node_payout_test_executions (
  payout_plan_id,
  mode,
  status,
  planned_amount_total,
  test_wallet_name,
  txid,
  execution_attempt_count,
  idempotency_key,
  notes
) VALUES (
  %(payout_plan_id)s,
  %(mode)s,
  %(status)s,
  %(planned_amount_total)s,
  %(test_wallet_name)s,
  %(txid)s,
  %(execution_attempt_count)s,
  %(idempotency_key)s,
  %(notes)s
)
RETURNING id
""".strip()
    _assert_test_insert_sql(sql)
    return sql


def build_insert_test_execution_row_sql() -> str:
    sql = """
INSERT INTO sc_node_payout_test_execution_rows (
  test_execution_id,
  payout_plan_row_id,
  sc_node_id,
  payout_address,
  payout_amount,
  row_status,
  txid
) VALUES (
  %(test_execution_id)s,
  %(payout_plan_row_id)s,
  %(sc_node_id)s,
  %(payout_address)s,
  %(payout_amount)s,
  %(row_status)s,
  %(txid)s
)
""".strip()
    _assert_test_insert_sql(sql)
    return sql


def build_update_execution_sent_sql() -> str:
    sql = """
UPDATE sc_node_payout_test_executions
SET status = 'sent',
    txid = %(txid)s,
    execution_attempt_count = execution_attempt_count + 1,
    updated_at = now()
WHERE id = %(test_execution_id)s
RETURNING id
""".strip()
    _assert_test_insert_sql(sql)
    return sql


def build_update_execution_rows_sent_sql() -> str:
    sql = """
UPDATE sc_node_payout_test_execution_rows
SET row_status = 'sent',
    txid = %(txid)s,
    updated_at = now()
WHERE test_execution_id = %(test_execution_id)s
  AND row_status = 'pending'
""".strip()
    _assert_test_insert_sql(sql)
    return sql


def build_update_execution_confirmed_sql() -> str:
    sql = """
UPDATE sc_node_payout_test_executions
SET status = 'confirmed',
    updated_at = now()
WHERE id = %(test_execution_id)s
  AND status = 'sent'
RETURNING id
""".strip()
    _assert_test_insert_sql(sql)
    return sql


def build_update_execution_rows_confirmed_sql() -> str:
    sql = """
UPDATE sc_node_payout_test_execution_rows
SET row_status = 'confirmed',
    updated_at = now()
WHERE test_execution_id = %(test_execution_id)s
  AND row_status = 'sent'
""".strip()
    _assert_test_insert_sql(sql)
    return sql


def build_test_executions_list_sql() -> str:
    sql = """
SELECT
  id,
  payout_plan_id,
  mode,
  status,
  planned_amount_total,
  test_wallet_name,
  txid,
  execution_attempt_count,
  idempotency_key,
  notes,
  created_at,
  updated_at
FROM sc_node_payout_test_executions
ORDER BY created_at DESC, id DESC
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_test_execution_details_sql(test_execution_id: int) -> str:
    safe_id = int(test_execution_id)
    sql = f"""
SELECT
  id,
  payout_plan_id,
  mode,
  status,
  planned_amount_total,
  test_wallet_name,
  txid,
  execution_attempt_count,
  idempotency_key,
  notes,
  created_at,
  updated_at
FROM sc_node_payout_test_executions
WHERE id = {safe_id}
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_test_execution_rows_sql(test_execution_id: int) -> str:
    safe_id = int(test_execution_id)
    sql = f"""
SELECT
  id,
  test_execution_id,
  payout_plan_row_id,
  sc_node_id,
  payout_address,
  payout_amount,
  row_status,
  txid,
  created_at,
  updated_at
FROM sc_node_payout_test_execution_rows
WHERE test_execution_id = {safe_id}
ORDER BY payout_amount DESC, sc_node_id
""".strip()
    _assert_readonly_sql(sql)
    return sql


def evaluate_preview_refusal(
    *,
    plan: Mapping[str, Any] | None,
    plan_rows: list[Mapping[str, Any]],
) -> str | None:
    if plan is None:
        return "payout plan not found"
    if str(plan.get("status")) != plan_review.PLAN_STATUS_APPROVED:
        return "payout plan status must be approved"
    if str(plan.get("preflight_status")) != plan_review.PREFLIGHT_STATUS_ALLOWED:
        return "payout plan preflight_status must be allowed"
    if not plan_rows:
        return "payout plan has no rows"
    for row in plan_rows:
        if str(row.get("row_status")) != plan_review.ROW_STATUS_APPROVED:
            return "all payout plan rows must be approved"
    return None


def evaluate_execute_fake_refusal(
    *,
    plan: Mapping[str, Any] | None,
    plan_rows: list[Mapping[str, Any]],
    mode: str,
    test_wallet_name: str,
    existing_by_key: Mapping[str, Any] | None,
    active_execution: Mapping[str, Any] | None,
    idempotency_key: str,
) -> str | None:
    preview_refusal = evaluate_preview_refusal(plan=plan, plan_rows=plan_rows)
    if preview_refusal:
        return preview_refusal
    if mode != EXECUTION_MODE_FAKE_REGTEST:
        return "execute-fake requires mode fake_regtest"
    assert plan is not None
    if is_production_wallet_name(str(plan.get("wallet_name") or "")):
        pass  # allowed to read plan; execution uses test_wallet_name only
    if existing_by_key is not None:
        return None
    if active_execution is not None:
        active_key = str(active_execution.get("idempotency_key"))
        if active_key != idempotency_key:
            return (
                "active test execution already exists for payout_plan_id "
                f"(execution id {active_execution.get('id')}, "
                f"idempotency_key {active_key})"
            )
    return None


def evaluate_mark_confirmed_refusal(
    execution: Mapping[str, Any] | None,
) -> str | None:
    if execution is None:
        return "test execution not found"
    status = str(execution.get("status"))
    if status == EXECUTION_STATUS_FAILED:
        return "cannot confirm failed test execution"
    if status == EXECUTION_STATUS_CONFIRMED:
        return None
    if status != EXECUTION_STATUS_SENT:
        return "test execution status must be sent to confirm"
    return None


def build_test_execution_preview(
    *,
    payout_plan_id: int,
    mode: str,
    test_wallet_name: str,
    plan: Mapping[str, Any] | None,
    plan_rows: list[Mapping[str, Any]],
) -> TestExecutionPreview:
    refusal = evaluate_preview_refusal(plan=plan, plan_rows=plan_rows)
    rows = [planner.row_to_payout_plan_row_dict(row) for row in plan_rows]
    planned = (
        planner._to_decimal(plan.get("planned_amount_total"))
        if plan is not None
        else Decimal("0")
    )
    return TestExecutionPreview(
        payout_plan_id=payout_plan_id,
        mode=mode,
        test_wallet_name=test_wallet_name,
        planned_amount_total=planned,
        row_count=len(rows),
        rows=tuple(rows),
        execution_allowed=refusal is None,
        refusal_reason=refusal,
    )


def test_execution_preview_to_dict(preview: TestExecutionPreview) -> dict[str, Any]:
    return {
        "payout_plan_id": preview.payout_plan_id,
        "mode": preview.mode,
        "test_wallet_name": preview.test_wallet_name,
        "planned_amount_total": planner._serialize_numeric(preview.planned_amount_total),
        "row_count": preview.row_count,
        "rows": list(preview.rows),
        "execution_allowed": preview.execution_allowed,
        "refusal_reason": preview.refusal_reason,
        "accounting_note": (
            "fake_regtest test harness only; does not move real coins or call wallet RPC"
        ),
    }


def row_to_test_execution_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": planner._to_int(row["id"]),
        "payout_plan_id": planner._to_int(row.get("payout_plan_id")),
        "mode": row.get("mode"),
        "status": row.get("status"),
        "planned_amount_total": planner._serialize_numeric(
            planner._to_decimal(row.get("planned_amount_total"))
        ),
        "test_wallet_name": row.get("test_wallet_name"),
        "txid": row.get("txid"),
        "execution_attempt_count": planner._to_int(row.get("execution_attempt_count")),
        "idempotency_key": row.get("idempotency_key"),
        "notes": row.get("notes"),
        "created_at": planner._serialize_datetime(row.get("created_at")),
        "updated_at": planner._serialize_datetime(row.get("updated_at")),
    }


def row_to_test_execution_row_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": planner._to_int(row["id"]),
        "test_execution_id": planner._to_int(row.get("test_execution_id")),
        "payout_plan_row_id": planner._to_int(row.get("payout_plan_row_id")),
        "sc_node_id": str(row["sc_node_id"]),
        "payout_address": str(row["payout_address"]),
        "payout_amount": planner._serialize_numeric(
            planner._to_decimal(row.get("payout_amount"))
        ),
        "row_status": row.get("row_status"),
        "txid": row.get("txid"),
        "created_at": planner._serialize_datetime(row.get("created_at")),
        "updated_at": planner._serialize_datetime(row.get("updated_at")),
    }
