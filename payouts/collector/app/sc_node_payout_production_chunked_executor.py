from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any, Mapping

from payouts.collector.app import sc_node_payout_planner as planner
from payouts.collector.app import sc_node_payout_production_executor as production_executor

CHUNKED_EXECUTOR_NOTES_PREFIX = "chunked_executor_v0"

EXECUTION_STATUS_PARTIAL_SENT = "partial_sent"

CHUNK_STATUS_DRAFT = "draft"
CHUNK_STATUS_SENT = "sent"
CHUNK_STATUS_CONFIRMED = "confirmed"
CHUNK_STATUS_REFUSED = "refused"

_CHUNKED_MUTATION_TABLES = frozenset(
    {
        "sc_node_payout_production_executions",
        "sc_node_payout_production_execution_rows",
        "sc_node_payout_production_execution_chunks",
    }
)

_READONLY_SQL_FORBIDDEN = production_executor._READONLY_SQL_FORBIDDEN  # noqa: SLF001


@dataclass(frozen=True)
class ChunkPlan:
    payout_plan_row_id: int
    sc_node_id: str
    payout_address: str
    chunk_index: int
    chunk_amount: Decimal


@dataclass(frozen=True)
class ChunkedExecutionPreview:
    payout_plan_id: int
    production_preflight_id: int
    source_wallet_name: str
    chunk_amount: Decimal
    planned_amount_total: Decimal
    chunk_count: int
    wallet_balance: production_executor.WalletBalance
    reserve_amount: Decimal
    spendable_after_reserve: Decimal
    expected_confirmation_phrase: str
    execution_allowed: bool
    refusal_reason: str | None
    rows: tuple[production_executor.ProductionExecutionRow, ...]
    chunks: tuple[ChunkPlan, ...]


def assert_no_forbidden_wallet_rpc_keywords_except_sendtoaddress(text: str) -> None:
    production_executor.assert_no_forbidden_wallet_rpc_keywords_except_sendtoaddress(text)


def _assert_readonly_sql(sql: str) -> None:
    if _READONLY_SQL_FORBIDDEN.search(sql):
        raise ValueError("SQL must be read-only SELECT only")


def _assert_chunked_mutation_sql(sql: str) -> None:
    assert_no_forbidden_wallet_rpc_keywords_except_sendtoaddress(sql)
    lowered = sql.lower()
    if "insert into" not in lowered and "update" not in lowered:
        raise ValueError("chunked execution SQL must INSERT or UPDATE")
    for token in re.findall(r"\b(?:insert\s+into|update)\s+([a-z0-9_]+)\b", lowered):
        if token not in _CHUNKED_MUTATION_TABLES:
            raise ValueError(f"chunked execution SQL must not target table: {token}")


def _quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000000000001"), rounding=ROUND_DOWN)


def normalize_chunk_amount(value: str | Decimal) -> Decimal:
    amount = _quantize_amount(planner._to_decimal(value))
    if amount <= 0:
        raise ValueError("chunk_amount must be greater than 0")
    return amount


def split_payout_amount_into_chunks(
    payout_amount: Decimal,
    chunk_amount: Decimal,
) -> tuple[Decimal, ...]:
    total = _quantize_amount(payout_amount)
    size = _quantize_amount(chunk_amount)
    if size <= 0:
        raise ValueError("chunk_amount must be greater than 0")
    if size > total:
        return (total,)
    chunks: list[Decimal] = []
    remaining = total
    while remaining > 0:
        if remaining <= size:
            chunks.append(remaining)
            break
        chunks.append(size)
        remaining = _quantize_amount(remaining - size)
    if sum(chunks, Decimal("0")) != total:
        raise ValueError("chunk amounts must sum exactly to payout_amount")
    return tuple(chunks)


def build_chunk_plans_for_rows(
    plan_rows: list[Mapping[str, Any]],
    chunk_amount: Decimal,
) -> tuple[ChunkPlan, ...]:
    plans: list[ChunkPlan] = []
    for row in plan_rows:
        payout_plan_row_id = planner._to_int(row["id"])
        sc_node_id = str(row["sc_node_id"])
        payout_address = str(row["payout_address"])
        amounts = split_payout_amount_into_chunks(
            planner._to_decimal(row.get("payout_amount")),
            chunk_amount,
        )
        for index, amount in enumerate(amounts, start=1):
            plans.append(
                ChunkPlan(
                    payout_plan_row_id=payout_plan_row_id,
                    sc_node_id=sc_node_id,
                    payout_address=payout_address,
                    chunk_index=index,
                    chunk_amount=amount,
                )
            )
    return tuple(plans)


def build_chunked_confirmation_phrase(
    *,
    payout_plan_id: int,
    planned_amount_total: Decimal,
    source_wallet_name: str,
    chunk_count: int,
) -> str:
    amount = planner._serialize_numeric(_quantize_amount(planned_amount_total))
    wallet = str(source_wallet_name).strip()
    if not wallet:
        raise ValueError("source_wallet_name is required")
    if chunk_count < 1:
        raise ValueError("chunk_count must be at least 1")
    return (
        f"SEND CHUNKED {amount} FROM {wallet} FOR PLAN {int(payout_plan_id)} "
        f"IN {int(chunk_count)} CHUNKS"
    )


def verify_chunked_confirmation_phrase(
    *,
    confirmation_phrase: str,
    payout_plan_id: int,
    planned_amount_total: Decimal,
    source_wallet_name: str,
    chunk_count: int,
) -> bool:
    expected = build_chunked_confirmation_phrase(
        payout_plan_id=payout_plan_id,
        planned_amount_total=planned_amount_total,
        source_wallet_name=source_wallet_name,
        chunk_count=chunk_count,
    )
    return confirmation_phrase.strip() == expected


def build_chunked_executor_notes(chunk_amount: Decimal) -> str:
    return f"{CHUNKED_EXECUTOR_NOTES_PREFIX} chunk_amount={planner._serialize_numeric(chunk_amount)}"


def is_chunked_execution_notes(notes: object) -> bool:
    if notes is None:
        return False
    return str(notes).startswith(CHUNKED_EXECUTOR_NOTES_PREFIX)


def build_existing_active_production_execution_sql() -> str:
    sql = """
SELECT
  id,
  payout_plan_id,
  production_preflight_id,
  source_wallet_name,
  status,
  planned_amount_total,
  idempotency_key,
  txid,
  refusal_reason,
  notes,
  created_at,
  updated_at
FROM sc_node_payout_production_executions
WHERE payout_plan_id = %(payout_plan_id)s
  AND status IN ('sent', 'confirmed', 'partial_sent')
ORDER BY id DESC
LIMIT 1
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_execution_by_plan_idempotency_sql() -> str:
    return production_executor.build_execution_by_plan_idempotency_sql()


def build_insert_production_execution_chunk_sql() -> str:
    sql = """
INSERT INTO sc_node_payout_production_execution_chunks (
  production_execution_id,
  production_execution_row_id,
  payout_plan_id,
  payout_plan_row_id,
  sc_node_id,
  payout_address,
  chunk_index,
  chunk_amount,
  chunk_status,
  txid,
  refusal_reason
) VALUES (
  %(production_execution_id)s,
  %(production_execution_row_id)s,
  %(payout_plan_id)s,
  %(payout_plan_row_id)s,
  %(sc_node_id)s,
  %(payout_address)s,
  %(chunk_index)s,
  %(chunk_amount)s,
  %(chunk_status)s,
  %(txid)s,
  %(refusal_reason)s
)
RETURNING id
""".strip()
    _assert_chunked_mutation_sql(sql)
    return sql


def build_production_execution_chunks_sql(production_execution_id: int) -> str:
    safe_id = int(production_execution_id)
    sql = f"""
SELECT
  id,
  production_execution_id,
  production_execution_row_id,
  payout_plan_id,
  payout_plan_row_id,
  sc_node_id,
  payout_address,
  chunk_index,
  chunk_amount,
  chunk_status,
  txid,
  refusal_reason,
  created_at,
  updated_at
FROM sc_node_payout_production_execution_chunks
WHERE production_execution_id = {safe_id}
ORDER BY production_execution_row_id, chunk_index
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_chunk_count_sql(production_execution_id: int) -> str:
    safe_id = int(production_execution_id)
    sql = f"""
SELECT COUNT(*)::bigint AS chunk_count
FROM sc_node_payout_production_execution_chunks
WHERE production_execution_id = {safe_id}
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_mark_chunk_sent_sql() -> str:
    sql = """
UPDATE sc_node_payout_production_execution_chunks
SET chunk_status = 'sent',
    txid = %(txid)s,
    updated_at = now()
WHERE id = %(chunk_id)s
  AND chunk_status = 'draft'
RETURNING id
""".strip()
    _assert_chunked_mutation_sql(sql)
    return sql


def build_mark_chunk_refused_sql() -> str:
    sql = """
UPDATE sc_node_payout_production_execution_chunks
SET chunk_status = 'refused',
    refusal_reason = %(refusal_reason)s,
    updated_at = now()
WHERE id = %(chunk_id)s
RETURNING id
""".strip()
    _assert_chunked_mutation_sql(sql)
    return sql


def build_mark_execution_partial_sent_sql() -> str:
    sql = """
UPDATE sc_node_payout_production_executions
SET status = 'partial_sent',
    refusal_reason = %(refusal_reason)s,
    execution_attempt_count = execution_attempt_count + 1,
    updated_at = now()
WHERE id = %(production_execution_id)s
RETURNING id
""".strip()
    _assert_chunked_mutation_sql(sql)
    return sql


def build_mark_chunked_execution_sent_sql() -> str:
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
    _assert_chunked_mutation_sql(sql)
    return sql


def build_mark_chunked_execution_row_sent_sql() -> str:
    sql = """
UPDATE sc_node_payout_production_execution_rows
SET row_status = 'sent',
    txid = %(txid)s,
    updated_at = now()
WHERE id = %(production_execution_row_id)s
  AND row_status = 'draft'
RETURNING id
""".strip()
    _assert_chunked_mutation_sql(sql)
    return sql


def build_mark_chunks_confirmed_sql() -> str:
    sql = """
UPDATE sc_node_payout_production_execution_chunks
SET chunk_status = 'confirmed',
    updated_at = now()
WHERE production_execution_id = %(production_execution_id)s
  AND chunk_status = 'sent'
""".strip()
    _assert_chunked_mutation_sql(sql)
    return sql


def build_mark_chunked_execution_confirmed_sql() -> str:
    sql = """
UPDATE sc_node_payout_production_executions
SET status = 'confirmed',
    updated_at = now()
WHERE id = %(production_execution_id)s
  AND status = 'sent'
RETURNING id
""".strip()
    _assert_chunked_mutation_sql(sql)
    return sql


def evaluate_chunked_execute_real_refusal(
    *,
    plan: Mapping[str, Any] | None,
    plan_rows: list[Mapping[str, Any]],
    preflight: Mapping[str, Any] | None,
    preflight_rows: list[Mapping[str, Any]],
    source_wallet_name: str,
    wallet_balance: production_executor.WalletBalance,
    address_lookup: dict[str, list[Mapping[str, Any]]],
    confirmation_phrase: str,
    chunk_amount: Decimal,
    chunks: tuple[ChunkPlan, ...],
    existing_by_key: Mapping[str, Any] | None,
    active_execution: Mapping[str, Any] | None,
    idempotency_key: str,
) -> str | None:
    if existing_by_key is not None:
        return None
    preview_refusal = production_executor.evaluate_preview_refusal(
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
    if not plan_rows:
        return "payout plan has no approved rows"
    planned_total = planner._to_decimal(plan.get("planned_amount_total"))
    if chunk_amount > planned_total:
        return "chunk_amount cannot exceed planned_amount_total"
    chunk_sum = sum((chunk.chunk_amount for chunk in chunks), Decimal("0"))
    if _quantize_amount(chunk_sum) != _quantize_amount(planned_total):
        return "chunk amounts must sum exactly to planned_amount_total"
    if not verify_chunked_confirmation_phrase(
        confirmation_phrase=confirmation_phrase,
        payout_plan_id=planner._to_int(plan.get("id")),
        planned_amount_total=planned_total,
        source_wallet_name=source_wallet_name,
        chunk_count=len(chunks),
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


def evaluate_chunked_mark_confirmed_refusal(
    execution: Mapping[str, Any] | None,
    *,
    chunk_count: int,
    sent_chunk_count: int,
) -> str | None:
    if execution is None:
        return "production execution not found"
    if chunk_count < 1:
        return "production execution has no chunks; use production executor mark-confirmed"
    if not is_chunked_execution_notes(execution.get("notes")):
        return "production execution is not a chunked execution"
    status = str(execution.get("status"))
    if status == production_executor.EXECUTION_STATUS_REFUSED:
        return "cannot confirm refused production execution"
    if status == EXECUTION_STATUS_PARTIAL_SENT:
        return "cannot confirm partial_sent chunked execution"
    if status == production_executor.EXECUTION_STATUS_CONFIRMED:
        return None
    if status != production_executor.EXECUTION_STATUS_SENT:
        return "chunked production execution status must be sent to confirm"
    if sent_chunk_count != chunk_count:
        return "all chunks must be sent before mark-confirmed"
    return None


def build_chunked_execution_preview(
    *,
    payout_plan_id: int,
    production_preflight_id: int,
    source_wallet_name: str,
    chunk_amount: Decimal,
    plan: Mapping[str, Any] | None,
    plan_rows: list[Mapping[str, Any]],
    preflight: Mapping[str, Any] | None,
    preflight_rows: list[Mapping[str, Any]],
    wallet_balance: production_executor.WalletBalance,
    address_lookup: dict[str, list[Mapping[str, Any]]],
) -> ChunkedExecutionPreview:
    base = production_executor.build_production_execution_preview(
        payout_plan_id=payout_plan_id,
        production_preflight_id=production_preflight_id,
        source_wallet_name=source_wallet_name,
        plan=plan,
        plan_rows=plan_rows,
        preflight=preflight,
        preflight_rows=preflight_rows,
        wallet_balance=wallet_balance,
        address_lookup=address_lookup,
    )
    chunks = build_chunk_plans_for_rows(plan_rows, chunk_amount)
    expected_phrase = build_chunked_confirmation_phrase(
        payout_plan_id=payout_plan_id,
        planned_amount_total=base.planned_amount_total,
        source_wallet_name=source_wallet_name,
        chunk_count=len(chunks),
    )
    refusal = base.refusal_reason
    if refusal is None and chunk_amount > base.planned_amount_total:
        refusal = "chunk_amount cannot exceed planned_amount_total"
    chunk_sum = sum((c.chunk_amount for c in chunks), Decimal("0"))
    if refusal is None and _quantize_amount(chunk_sum) != base.planned_amount_total:
        refusal = "chunk amounts must sum exactly to planned_amount_total"
    return ChunkedExecutionPreview(
        payout_plan_id=payout_plan_id,
        production_preflight_id=production_preflight_id,
        source_wallet_name=source_wallet_name,
        chunk_amount=chunk_amount,
        planned_amount_total=base.planned_amount_total,
        chunk_count=len(chunks),
        wallet_balance=base.wallet_balance,
        reserve_amount=base.reserve_amount,
        spendable_after_reserve=base.spendable_after_reserve,
        expected_confirmation_phrase=expected_phrase,
        execution_allowed=refusal is None,
        refusal_reason=refusal,
        rows=base.rows,
        chunks=chunks,
    )


def chunked_execution_preview_to_dict(preview: ChunkedExecutionPreview) -> dict[str, Any]:
    base = production_executor.production_execution_preview_to_dict(
        production_executor.ProductionExecutionPreview(
            payout_plan_id=preview.payout_plan_id,
            production_preflight_id=preview.production_preflight_id,
            source_wallet_name=preview.source_wallet_name,
            planned_amount_total=preview.planned_amount_total,
            row_count=len(preview.rows),
            wallet_balance=preview.wallet_balance,
            reserve_amount=preview.reserve_amount,
            spendable_after_reserve=preview.spendable_after_reserve,
            expected_confirmation_phrase=preview.expected_confirmation_phrase,
            execution_allowed=preview.execution_allowed,
            refusal_reason=preview.refusal_reason,
            rows=preview.rows,
        )
    )
    base["chunk_amount"] = planner._serialize_numeric(preview.chunk_amount)
    base["chunk_count"] = preview.chunk_count
    base["expected_confirmation_phrase"] = preview.expected_confirmation_phrase
    base["chunks"] = [
        {
            "payout_plan_row_id": chunk.payout_plan_row_id,
            "sc_node_id": chunk.sc_node_id,
            "payout_address": chunk.payout_address,
            "chunk_index": chunk.chunk_index,
            "chunk_amount": planner._serialize_numeric(chunk.chunk_amount),
        }
        for chunk in preview.chunks
    ]
    base["accounting_note"] = (
        "chunked preview only; execute-real sends sequential sendtoaddress per chunk"
    )
    return base


def row_to_production_execution_chunk_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": planner._to_int(row["id"]),
        "production_execution_id": planner._to_int(row.get("production_execution_id")),
        "production_execution_row_id": planner._to_int(
            row.get("production_execution_row_id")
        ),
        "payout_plan_id": planner._to_int(row.get("payout_plan_id")),
        "payout_plan_row_id": planner._to_int(row.get("payout_plan_row_id")),
        "sc_node_id": str(row["sc_node_id"]),
        "payout_address": str(row["payout_address"]),
        "chunk_index": planner._to_int(row.get("chunk_index")),
        "chunk_amount": planner._serialize_numeric(planner._to_decimal(row.get("chunk_amount"))),
        "chunk_status": row.get("chunk_status"),
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
    return production_executor.build_sendtoaddress_argv(
        azc_bin=azc_bin,
        source_wallet_name=source_wallet_name,
        payout_address=payout_address,
        payout_amount=payout_amount,
    )
