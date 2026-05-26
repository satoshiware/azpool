from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any, Mapping

from payouts.collector.app import sc_node_payout_planner as planner

RECONCILIATION_STATUS_DRAFT = "draft"
RECONCILIATION_STATUS_MATCHED = "matched"
RECONCILIATION_STATUS_MISMATCH = "mismatch"
RECONCILIATION_STATUS_VOID = "void"

ROW_STATUS_DRAFT = "draft"
ROW_STATUS_MATCHED = "matched"
ROW_STATUS_MISMATCH = "mismatch"
ROW_STATUS_VOID = "void"

EXECUTION_STATUS_CONFIRMED = "confirmed"

RECEIVER_CATEGORY_RECEIVE = "receive"

_RECEIVER_MISSING_REASON = "receiver evidence missing"

_RECONCILIATION_INSERT_TABLES = frozenset(
    {
        "sc_node_payout_reconciliations",
        "sc_node_payout_reconciliation_rows",
    }
)

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


@dataclass(frozen=True)
class SourceTransactionEvidence:
    txid: str
    confirmations: int
    fee: Decimal | None
    amount: Decimal | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class ReceiverTransactionEvidence:
    txid: str
    confirmations: int
    amount: Decimal
    category: str
    address: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class ReconciliationRowPreview:
    production_execution_row_id: int
    sc_node_id: str
    expected_address: str
    expected_amount: Decimal
    receiver_address: str | None
    receiver_amount: Decimal | None
    receiver_category: str | None
    receiver_confirmations: int | None
    row_status: str
    mismatch_reason: str | None


@dataclass(frozen=True)
class ReconciliationPreview:
    production_execution_id: int
    payout_plan_id: int
    source_wallet_name: str
    txid: str
    reconciliation_status: str
    expected_amount: Decimal
    expected_address: str
    source_confirmations: int | None
    source_fee: Decimal | None
    source_amount: Decimal | None
    receiver_confirmations: int | None
    receiver_amount: Decimal | None
    receiver_category: str | None
    receiver_address: str | None
    matched: bool
    mismatch_reason: str | None
    source_wallet_evidence: dict[str, Any]
    receiver_wallet_evidence: dict[str, Any] | None
    rows: tuple[ReconciliationRowPreview, ...]


def assert_no_wallet_send_keywords(text: str) -> None:
    if _WALLET_SEND_KEYWORDS.search(text):
        raise ValueError("text must not contain wallet send or signing keywords")


def _assert_readonly_sql(sql: str) -> None:
    if _READONLY_SQL_FORBIDDEN.search(sql):
        raise ValueError("SQL must be read-only SELECT only")


def _assert_reconciliation_insert_sql(sql: str) -> None:
    assert_no_wallet_send_keywords(sql)
    lowered = sql.lower()
    if "insert into" not in lowered:
        raise ValueError("reconciliation SQL must INSERT")
    for token in re.findall(r"insert\s+into\s+([a-z0-9_]+)", lowered):
        if token not in _RECONCILIATION_INSERT_TABLES:
            raise ValueError(f"reconciliation SQL must not target table: {token}")


def _quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000000000001"), rounding=ROUND_DOWN)


def _to_decimal(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def normalize_source_wallet_name(value: str) -> str:
    name = str(value).strip()
    if not name:
        raise ValueError("source_wallet_name is required")
    return name


def build_confirmed_production_execution_sql(production_execution_id: int) -> str:
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
  AND status = '{EXECUTION_STATUS_CONFIRMED}'
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_confirmed_production_execution_rows_sql(production_execution_id: int) -> str:
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
  AND row_status = '{EXECUTION_STATUS_CONFIRMED}'
ORDER BY payout_amount DESC, sc_node_id
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_insert_reconciliation_sql() -> str:
    sql = """
INSERT INTO sc_node_payout_reconciliations (
  production_execution_id,
  payout_plan_id,
  source_wallet_name,
  txid,
  reconciliation_status,
  expected_amount,
  expected_address,
  source_confirmations,
  source_fee,
  source_amount,
  receiver_confirmations,
  receiver_amount,
  receiver_category,
  receiver_address,
  matched,
  mismatch_reason,
  source_wallet_evidence,
  receiver_wallet_evidence,
  notes
) VALUES (
  %(production_execution_id)s,
  %(payout_plan_id)s,
  %(source_wallet_name)s,
  %(txid)s,
  %(reconciliation_status)s,
  %(expected_amount)s,
  %(expected_address)s,
  %(source_confirmations)s,
  %(source_fee)s,
  %(source_amount)s,
  %(receiver_confirmations)s,
  %(receiver_amount)s,
  %(receiver_category)s,
  %(receiver_address)s,
  %(matched)s,
  %(mismatch_reason)s,
  %(source_wallet_evidence)s,
  %(receiver_wallet_evidence)s,
  %(notes)s
)
RETURNING id
""".strip()
    _assert_reconciliation_insert_sql(sql)
    return sql


def build_insert_reconciliation_row_sql() -> str:
    sql = """
INSERT INTO sc_node_payout_reconciliation_rows (
  reconciliation_id,
  production_execution_row_id,
  sc_node_id,
  expected_address,
  expected_amount,
  receiver_address,
  receiver_amount,
  receiver_category,
  receiver_confirmations,
  row_status,
  mismatch_reason
) VALUES (
  %(reconciliation_id)s,
  %(production_execution_row_id)s,
  %(sc_node_id)s,
  %(expected_address)s,
  %(expected_amount)s,
  %(receiver_address)s,
  %(receiver_amount)s,
  %(receiver_category)s,
  %(receiver_confirmations)s,
  %(row_status)s,
  %(mismatch_reason)s
)
""".strip()
    _assert_reconciliation_insert_sql(sql)
    return sql


def build_reconciliations_sql() -> str:
    sql = """
SELECT
  id,
  production_execution_id,
  payout_plan_id,
  source_wallet_name,
  txid,
  reconciliation_status,
  expected_amount,
  expected_address,
  source_confirmations,
  source_fee,
  source_amount,
  receiver_confirmations,
  receiver_amount,
  receiver_category,
  receiver_address,
  matched,
  mismatch_reason,
  source_wallet_evidence,
  receiver_wallet_evidence,
  notes,
  created_at,
  updated_at
FROM sc_node_payout_reconciliations
ORDER BY created_at DESC, id DESC
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_reconciliation_details_sql(reconciliation_id: int) -> str:
    safe_id = int(reconciliation_id)
    sql = f"""
SELECT
  id,
  production_execution_id,
  payout_plan_id,
  source_wallet_name,
  txid,
  reconciliation_status,
  expected_amount,
  expected_address,
  source_confirmations,
  source_fee,
  source_amount,
  receiver_confirmations,
  receiver_amount,
  receiver_category,
  receiver_address,
  matched,
  mismatch_reason,
  source_wallet_evidence,
  receiver_wallet_evidence,
  notes,
  created_at,
  updated_at
FROM sc_node_payout_reconciliations
WHERE id = {safe_id}
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_reconciliation_rows_sql(reconciliation_id: int) -> str:
    safe_id = int(reconciliation_id)
    sql = f"""
SELECT
  id,
  reconciliation_id,
  production_execution_row_id,
  sc_node_id,
  expected_address,
  expected_amount,
  receiver_address,
  receiver_amount,
  receiver_category,
  receiver_confirmations,
  row_status,
  mismatch_reason,
  created_at,
  updated_at
FROM sc_node_payout_reconciliation_rows
WHERE reconciliation_id = {safe_id}
ORDER BY expected_amount DESC, sc_node_id
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_reconciliation_by_execution_txid_sql() -> str:
    sql = """
SELECT
  id,
  production_execution_id,
  payout_plan_id,
  source_wallet_name,
  txid,
  reconciliation_status,
  expected_amount,
  expected_address,
  matched,
  mismatch_reason,
  created_at,
  updated_at
FROM sc_node_payout_reconciliations
WHERE production_execution_id = %(production_execution_id)s
  AND txid = %(txid)s
""".strip()
    _assert_readonly_sql(sql)
    return sql


def parse_source_gettransaction(payload: Mapping[str, Any], txid: str) -> SourceTransactionEvidence:
    expected_txid = str(txid).strip()
    if not expected_txid:
        raise ValueError("txid is required")
    payload_txid = str(payload.get("txid", "")).strip()
    if payload_txid and payload_txid != expected_txid:
        raise ValueError("gettransaction txid does not match expected txid")

    confirmations_raw = payload.get("confirmations", 0)
    try:
        confirmations = int(confirmations_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("confirmations must be an integer") from exc
    if confirmations < 0:
        raise ValueError("confirmations must be non-negative")

    fee: Decimal | None = None
    if payload.get("fee") is not None:
        fee = _quantize_amount(abs(_to_decimal(payload.get("fee"))))

    amount: Decimal | None = None
    if payload.get("amount") is not None:
        amount = _quantize_amount(abs(_to_decimal(payload.get("amount"))))

    details = payload.get("details")
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, Mapping):
                continue
            detail_amount = detail.get("amount")
            if detail_amount is None:
                continue
            quantized = _quantize_amount(abs(_to_decimal(detail_amount)))
            if amount is None or quantized > amount:
                amount = quantized

    raw = dict(payload)
    return SourceTransactionEvidence(
        txid=payload_txid or expected_txid,
        confirmations=confirmations,
        fee=fee,
        amount=amount,
        raw=raw,
    )


def parse_receiver_transactions_json(
    rows: list[dict[str, Any]],
    txid: str,
) -> ReceiverTransactionEvidence | None:
    expected_txid = str(txid).strip()
    if not expected_txid:
        raise ValueError("txid is required")
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        row_txid = str(row.get("txid", "")).strip()
        if row_txid != expected_txid:
            continue
        address = str(row.get("address", "")).strip()
        if not address:
            raise ValueError("receiver row must include address")
        category = str(row.get("category", "")).strip().lower()
        amount = _quantize_amount(_to_decimal(row.get("amount")))
        try:
            confirmations = int(row.get("confirmations", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("receiver confirmations must be an integer") from exc
        if confirmations < 0:
            raise ValueError("receiver confirmations must be non-negative")
        return ReceiverTransactionEvidence(
            txid=row_txid,
            confirmations=confirmations,
            amount=amount,
            category=category,
            address=address,
            raw=dict(row),
        )
    return None


def _header_expected_address(rows: list[Mapping[str, Any]]) -> str:
    if not rows:
        return ""
    return str(rows[0]["payout_address"]).strip()


def _header_expected_amount(rows: list[Mapping[str, Any]]) -> Decimal:
    total = Decimal("0")
    for row in rows:
        total += _quantize_amount(_to_decimal(row.get("payout_amount")))
    return _quantize_amount(total)


def _compare_row(
    *,
    execution_row: Mapping[str, Any],
    receiver_evidence: ReceiverTransactionEvidence | None,
    execution_txid: str,
) -> ReconciliationRowPreview:
    row_id = int(execution_row["id"])
    sc_node_id = str(execution_row["sc_node_id"])
    expected_address = str(execution_row["payout_address"]).strip()
    expected_amount = _quantize_amount(_to_decimal(execution_row.get("payout_amount")))

    if receiver_evidence is None:
        return ReconciliationRowPreview(
            production_execution_row_id=row_id,
            sc_node_id=sc_node_id,
            expected_address=expected_address,
            expected_amount=expected_amount,
            receiver_address=None,
            receiver_amount=None,
            receiver_category=None,
            receiver_confirmations=None,
            row_status=ROW_STATUS_DRAFT,
            mismatch_reason=_RECEIVER_MISSING_REASON,
        )

    reasons: list[str] = []
    if receiver_evidence.txid != execution_txid:
        reasons.append("receiver txid mismatch")
    if receiver_evidence.category != RECEIVER_CATEGORY_RECEIVE:
        reasons.append("receiver category must be receive")
    if receiver_evidence.address != expected_address:
        reasons.append("receiver address mismatch")
    if receiver_evidence.amount != expected_amount:
        reasons.append("receiver amount mismatch")

    if reasons:
        return ReconciliationRowPreview(
            production_execution_row_id=row_id,
            sc_node_id=sc_node_id,
            expected_address=expected_address,
            expected_amount=expected_amount,
            receiver_address=receiver_evidence.address,
            receiver_amount=receiver_evidence.amount,
            receiver_category=receiver_evidence.category,
            receiver_confirmations=receiver_evidence.confirmations,
            row_status=ROW_STATUS_MISMATCH,
            mismatch_reason="; ".join(reasons),
        )

    return ReconciliationRowPreview(
        production_execution_row_id=row_id,
        sc_node_id=sc_node_id,
        expected_address=expected_address,
        expected_amount=expected_amount,
        receiver_address=receiver_evidence.address,
        receiver_amount=receiver_evidence.amount,
        receiver_category=receiver_evidence.category,
        receiver_confirmations=receiver_evidence.confirmations,
        row_status=ROW_STATUS_MATCHED,
        mismatch_reason=None,
    )


def compare_reconciliation(
    execution: Mapping[str, Any],
    execution_rows: list[Mapping[str, Any]],
    source_evidence: SourceTransactionEvidence,
    receiver_evidence: ReceiverTransactionEvidence | None,
) -> ReconciliationPreview:
    production_execution_id = int(execution["id"])
    payout_plan_id = int(execution["payout_plan_id"])
    source_wallet_name = str(execution["source_wallet_name"]).strip()
    execution_status = str(execution.get("status", "")).strip()
    execution_txid = str(execution.get("txid", "")).strip()

    if not execution_txid:
        raise ValueError("production execution txid is required for reconciliation")

    header_reasons: list[str] = []

    if execution_status != EXECUTION_STATUS_CONFIRMED:
        header_reasons.append("production execution is not confirmed")

    if source_evidence.txid != execution_txid:
        header_reasons.append("source txid mismatch")

    if source_evidence.confirmations < 1:
        header_reasons.append("source confirmations pending")

    row_previews = tuple(
        _compare_row(
            execution_row=row,
            receiver_evidence=receiver_evidence,
            execution_txid=execution_txid,
        )
        for row in execution_rows
    )

    if receiver_evidence is None:
        header_reasons.append(_RECEIVER_MISSING_REASON)
    elif receiver_evidence.txid != execution_txid:
        header_reasons.append("receiver txid mismatch")

    hard_mismatch_reasons = [
        reason for reason in header_reasons if reason != _RECEIVER_MISSING_REASON
    ]
    row_mismatch = any(
        preview.row_status == ROW_STATUS_MISMATCH for preview in row_previews
    )

    if hard_mismatch_reasons or row_mismatch:
        reconciliation_status = RECONCILIATION_STATUS_MISMATCH
        matched = False
    elif receiver_evidence is None:
        reconciliation_status = RECONCILIATION_STATUS_DRAFT
        matched = False
    else:
        reconciliation_status = RECONCILIATION_STATUS_MATCHED
        matched = True

    mismatch_reason = "; ".join(header_reasons) if header_reasons else None

    receiver_confirmations = (
        receiver_evidence.confirmations if receiver_evidence is not None else None
    )
    receiver_amount = receiver_evidence.amount if receiver_evidence is not None else None
    receiver_category = (
        receiver_evidence.category if receiver_evidence is not None else None
    )
    receiver_address = (
        receiver_evidence.address if receiver_evidence is not None else None
    )

    return ReconciliationPreview(
        production_execution_id=production_execution_id,
        payout_plan_id=payout_plan_id,
        source_wallet_name=source_wallet_name,
        txid=execution_txid,
        reconciliation_status=reconciliation_status,
        expected_amount=_header_expected_amount(execution_rows),
        expected_address=_header_expected_address(execution_rows),
        source_confirmations=source_evidence.confirmations,
        source_fee=source_evidence.fee,
        source_amount=source_evidence.amount,
        receiver_confirmations=receiver_confirmations,
        receiver_amount=receiver_amount,
        receiver_category=receiver_category,
        receiver_address=receiver_address,
        matched=matched,
        mismatch_reason=mismatch_reason,
        source_wallet_evidence=source_evidence.raw,
        receiver_wallet_evidence=(
            receiver_evidence.raw if receiver_evidence is not None else None
        ),
        rows=row_previews,
    )


def reconciliation_preview_to_dict(preview: ReconciliationPreview) -> dict[str, Any]:
    return {
        "production_execution_id": preview.production_execution_id,
        "payout_plan_id": preview.payout_plan_id,
        "source_wallet_name": preview.source_wallet_name,
        "txid": preview.txid,
        "reconciliation_status": preview.reconciliation_status,
        "expected_amount": planner._serialize_numeric(preview.expected_amount),
        "expected_address": preview.expected_address,
        "source_confirmations": preview.source_confirmations,
        "source_fee": (
            planner._serialize_numeric(preview.source_fee)
            if preview.source_fee is not None
            else None
        ),
        "source_amount": (
            planner._serialize_numeric(preview.source_amount)
            if preview.source_amount is not None
            else None
        ),
        "receiver_confirmations": preview.receiver_confirmations,
        "receiver_amount": (
            planner._serialize_numeric(preview.receiver_amount)
            if preview.receiver_amount is not None
            else None
        ),
        "receiver_category": preview.receiver_category,
        "receiver_address": preview.receiver_address,
        "matched": preview.matched,
        "mismatch_reason": preview.mismatch_reason,
        "source_wallet_evidence": preview.source_wallet_evidence,
        "receiver_wallet_evidence": preview.receiver_wallet_evidence,
        "rows": [
            {
                "production_execution_row_id": row.production_execution_row_id,
                "sc_node_id": row.sc_node_id,
                "expected_address": row.expected_address,
                "expected_amount": planner._serialize_numeric(row.expected_amount),
                "receiver_address": row.receiver_address,
                "receiver_amount": (
                    planner._serialize_numeric(row.receiver_amount)
                    if row.receiver_amount is not None
                    else None
                ),
                "receiver_category": row.receiver_category,
                "receiver_confirmations": row.receiver_confirmations,
                "row_status": row.row_status,
                "mismatch_reason": row.mismatch_reason,
            }
            for row in preview.rows
        ],
    }


def row_to_reconciliation_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "production_execution_id": int(row["production_execution_id"]),
        "payout_plan_id": int(row["payout_plan_id"]),
        "source_wallet_name": str(row["source_wallet_name"]),
        "txid": row.get("txid"),
        "reconciliation_status": row.get("reconciliation_status"),
        "expected_amount": planner._serialize_numeric(_to_decimal(row.get("expected_amount"))),
        "expected_address": row.get("expected_address"),
        "source_confirmations": (
            int(row["source_confirmations"])
            if row.get("source_confirmations") is not None
            else None
        ),
        "source_fee": (
            planner._serialize_numeric(_to_decimal(row["source_fee"]))
            if row.get("source_fee") is not None
            else None
        ),
        "source_amount": (
            planner._serialize_numeric(_to_decimal(row["source_amount"]))
            if row.get("source_amount") is not None
            else None
        ),
        "receiver_confirmations": (
            int(row["receiver_confirmations"])
            if row.get("receiver_confirmations") is not None
            else None
        ),
        "receiver_amount": (
            planner._serialize_numeric(_to_decimal(row["receiver_amount"]))
            if row.get("receiver_amount") is not None
            else None
        ),
        "receiver_category": row.get("receiver_category"),
        "receiver_address": row.get("receiver_address"),
        "matched": bool(row.get("matched")),
        "mismatch_reason": row.get("mismatch_reason"),
        "source_wallet_evidence": row.get("source_wallet_evidence"),
        "receiver_wallet_evidence": row.get("receiver_wallet_evidence"),
        "notes": row.get("notes"),
        "created_at": planner._serialize_datetime(row.get("created_at")),
        "updated_at": planner._serialize_datetime(row.get("updated_at")),
    }


def row_to_reconciliation_row_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "reconciliation_id": int(row["reconciliation_id"]),
        "production_execution_row_id": int(row["production_execution_row_id"]),
        "sc_node_id": str(row["sc_node_id"]),
        "expected_address": row.get("expected_address"),
        "expected_amount": planner._serialize_numeric(_to_decimal(row.get("expected_amount"))),
        "receiver_address": row.get("receiver_address"),
        "receiver_amount": (
            planner._serialize_numeric(_to_decimal(row["receiver_amount"]))
            if row.get("receiver_amount") is not None
            else None
        ),
        "receiver_category": row.get("receiver_category"),
        "receiver_confirmations": (
            int(row["receiver_confirmations"])
            if row.get("receiver_confirmations") is not None
            else None
        ),
        "row_status": row.get("row_status"),
        "mismatch_reason": row.get("mismatch_reason"),
        "created_at": planner._serialize_datetime(row.get("created_at")),
        "updated_at": planner._serialize_datetime(row.get("updated_at")),
    }
