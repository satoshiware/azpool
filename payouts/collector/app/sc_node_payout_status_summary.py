from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Any, Mapping

from payouts.collector.app import admin_readonly
from payouts.collector.app import (
    sc_node_chunked_payout_reconciliation as chunked_recon,
    sc_node_payout_production_chunked_executor as chunked_executor,
    sc_node_payout_production_executor as production_executor,
    sc_node_payout_reconciliation as single_reconciliation,
)
from payouts.collector.app import sc_node_payout_planner as planner


def build_payout_status_summary_execution_sql(production_execution_id: int) -> str:
    sql = production_executor.build_production_execution_details_sql(production_execution_id)
    admin_readonly.assert_readonly_sql(sql)
    return sql


def build_payout_status_summary_rows_sql(production_execution_id: int) -> str:
    sql = production_executor.build_production_execution_rows_sql(production_execution_id)
    admin_readonly.assert_readonly_sql(sql)
    return sql


def build_payout_status_summary_chunks_sql(production_execution_id: int) -> str:
    sql = chunked_executor.build_production_execution_chunks_sql(production_execution_id)
    admin_readonly.assert_readonly_sql(sql)
    return sql


def build_payout_status_summary_active_chunked_reconciliation_sql() -> str:
    sql = chunked_recon.build_chunked_reconciliation_by_execution_sql()
    admin_readonly.assert_readonly_sql(sql)
    return sql


def build_payout_status_summary_single_reconciliation_sql() -> str:
    sql = single_reconciliation.build_reconciliation_by_execution_txid_sql()
    admin_readonly.assert_readonly_sql(sql)
    return sql


def _serialize_decimal(value: object) -> str:
    if value is None:
        return "0"
    if isinstance(value, Decimal):
        return planner._serialize_numeric(value)
    return planner._serialize_numeric(Decimal(str(value)))


def _chunk_status_counts(chunks: list[Mapping[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for chunk in chunks:
        status = str(chunk.get("chunk_status", "unknown")).strip() or "unknown"
        counter[status] += 1
    return dict(sorted(counter.items()))


def _compact_chunked_reconciliation(row: Mapping[str, Any]) -> dict[str, Any]:
    compact = chunked_recon.row_to_chunked_reconciliation_dict(
        row,
        include_raw_evidence=False,
    )
    return {
        "kind": "chunked",
        "reconciliation_id": compact["id"],
        "production_execution_id": compact["production_execution_id"],
        "payout_plan_id": compact["payout_plan_id"],
        "sc_node_id": compact["sc_node_id"],
        "payout_address": compact.get("payout_address"),
        "reconciliation_status": compact.get("reconciliation_status"),
        "matched": compact.get("matched"),
        "is_active": compact.get("is_active"),
        "refusal_reason": compact.get("refusal_reason"),
        "expected_chunk_count": compact.get("expected_chunk_count"),
        "source_chunk_count": compact.get("source_chunk_count"),
        "receiver_chunk_count": compact.get("receiver_chunk_count"),
        "expected_amount_total": compact.get("expected_amount_total"),
        "source_amount_total": compact.get("source_amount_total"),
        "receiver_amount_total": compact.get("receiver_amount_total"),
        "source_fee_total": compact.get("source_fee_total"),
        "supersedes_reconciliation_id": compact.get("supersedes_reconciliation_id"),
        "superseded_by_reconciliation_id": compact.get("superseded_by_reconciliation_id"),
        "superseded_reason": compact.get("superseded_reason"),
        "superseded_at": compact.get("superseded_at"),
    }


def _compact_single_reconciliation(row: Mapping[str, Any]) -> dict[str, Any]:
    compact = single_reconciliation.row_to_reconciliation_dict(
        row,
        include_raw_evidence=False,
    )
    return {
        "kind": "single",
        "reconciliation_id": compact["id"],
        "production_execution_id": compact["production_execution_id"],
        "payout_plan_id": compact["payout_plan_id"],
        "txid": compact.get("txid"),
        "reconciliation_status": compact.get("reconciliation_status"),
        "matched": compact.get("matched"),
        "is_active": True,
        "mismatch_reason": compact.get("mismatch_reason"),
        "expected_amount": compact.get("expected_amount"),
        "source_amount": compact.get("source_amount"),
        "receiver_amount": compact.get("receiver_amount"),
        "source_fee": compact.get("source_fee"),
        "expected_address": compact.get("expected_address"),
        "receiver_address": compact.get("receiver_address"),
    }


def build_payout_status_summary(
    *,
    execution: Mapping[str, Any],
    execution_rows: list[Mapping[str, Any]],
    chunks: list[Mapping[str, Any]],
    active_chunked_reconciliation: Mapping[str, Any] | None,
    single_reconciliation_row: Mapping[str, Any] | None,
) -> dict[str, Any]:
    execution_dict = production_executor.row_to_production_execution_dict(execution)
    is_chunked = chunked_executor.is_chunked_execution_notes(execution.get("notes")) or bool(
        chunks
    )
    chunk_status_counts = _chunk_status_counts(chunks) if chunks else {}
    if active_chunked_reconciliation is not None:
        reconciliation = _compact_chunked_reconciliation(active_chunked_reconciliation)
    elif single_reconciliation_row is not None:
        reconciliation = _compact_single_reconciliation(single_reconciliation_row)
    else:
        reconciliation = None

    planned_total = _serialize_decimal(execution.get("planned_amount_total"))
    chunk_amount_total = "0"
    if chunks:
        total = Decimal("0")
        for chunk in chunks:
            total += Decimal(str(chunk.get("chunk_amount", "0")))
        chunk_amount_total = _serialize_decimal(total)

    summary: dict[str, Any] = {
        "command": "payout-status-summary",
        "production_execution_id": int(execution["id"]),
        "payout_plan_id": int(execution["payout_plan_id"]),
        "production_preflight_id": int(execution["production_preflight_id"]),
        "execution_status": execution_dict.get("status"),
        "source_wallet_name": execution_dict.get("source_wallet_name"),
        "planned_amount_total": planned_total,
        "execution_txid": execution_dict.get("txid"),
        "refusal_reason": execution_dict.get("refusal_reason"),
        "is_chunked_execution": is_chunked,
        "execution_row_count": len(execution_rows),
        "execution_rows": [
            production_executor.row_to_production_execution_row_dict(row)
            for row in execution_rows
        ],
    }

    if is_chunked:
        summary["chunk_summary"] = {
            "chunk_count": len(chunks),
            "chunk_status_counts": chunk_status_counts,
            "chunk_amount_total": chunk_amount_total,
            "chunks": [
                chunked_executor.row_to_production_execution_chunk_dict(chunk)
                for chunk in chunks
            ],
        }
    else:
        summary["chunk_summary"] = None

    summary["active_reconciliation"] = reconciliation
    return summary
