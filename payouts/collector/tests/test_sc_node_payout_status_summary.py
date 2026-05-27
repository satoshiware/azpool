from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

from payouts.collector.app import admin_readonly
from payouts.collector.app import sc_node_payout_status_summary as status_summary


_EXECUTION_ID = 3
_PLAN_ID = 2
_PREFLIGHT_ID = 2
_ADDRESS = "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv"
_NOW = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)


def _execution(*, notes: str | None = None, txid: str | None = "tx-main") -> dict[str, object]:
    return {
        "id": _EXECUTION_ID,
        "payout_plan_id": _PLAN_ID,
        "production_preflight_id": _PREFLIGHT_ID,
        "source_wallet_name": "wallet",
        "status": "confirmed",
        "planned_amount_total": Decimal("223.125000000000"),
        "trusted_balance_before": Decimal("500"),
        "immature_balance_before": Decimal("0"),
        "reserve_amount": Decimal("250"),
        "spendable_after_reserve": Decimal("250"),
        "execution_attempt_count": 1,
        "idempotency_key": "production-chunked-v0-plan-2",
        "confirmation_phrase": "SEND CHUNKED 223.125000000000 FROM wallet FOR PLAN 2 IN 9 CHUNKS",
        "txid": txid,
        "refusal_reason": None,
        "notes": notes,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _execution_row() -> dict[str, object]:
    return {
        "id": 10,
        "production_execution_id": _EXECUTION_ID,
        "payout_plan_row_id": 20,
        "sc_node_id": "sc-2",
        "payout_address": _ADDRESS,
        "payout_amount": Decimal("223.125000000000"),
        "row_status": "confirmed",
        "txid": "tx-chunk-0",
        "refusal_reason": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _chunk(index: int, *, status: str = "confirmed", txid: str | None = None) -> dict[str, object]:
    return {
        "id": 100 + index,
        "production_execution_id": _EXECUTION_ID,
        "production_execution_row_id": 10,
        "payout_plan_id": _PLAN_ID,
        "payout_plan_row_id": 20,
        "sc_node_id": "sc-2",
        "payout_address": _ADDRESS,
        "chunk_index": index,
        "chunk_amount": Decimal("25.000000000000"),
        "chunk_status": status,
        "txid": txid or f"tx-chunk-{index}",
        "refusal_reason": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _active_chunked_reconciliation() -> dict[str, object]:
    return {
        "id": 2,
        "production_execution_id": _EXECUTION_ID,
        "payout_plan_id": _PLAN_ID,
        "sc_node_id": "sc-2",
        "payout_address": _ADDRESS,
        "expected_chunk_count": 9,
        "source_chunk_count": 9,
        "receiver_chunk_count": 9,
        "expected_amount_total": Decimal("223.125000000000"),
        "source_amount_total": Decimal("223.125000000000"),
        "source_fee_total": Decimal("0.001"),
        "receiver_amount_total": Decimal("223.125000000000"),
        "reconciliation_status": "matched",
        "matched": True,
        "refusal_reason": None,
        "source_wallet_name": "wallet",
        "source_wallet_evidence": None,
        "receiver_wallet_evidence": None,
        "superseded_at": None,
        "superseded_by_reconciliation_id": None,
        "superseded_reason": None,
        "supersedes_reconciliation_id": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def test_status_summary_sql_builders_are_select_only() -> None:
    for sql in (
        status_summary.build_payout_status_summary_execution_sql(_EXECUTION_ID),
        status_summary.build_payout_status_summary_rows_sql(_EXECUTION_ID),
        status_summary.build_payout_status_summary_chunks_sql(_EXECUTION_ID),
        status_summary.build_payout_status_summary_active_chunked_reconciliation_sql(),
        status_summary.build_payout_status_summary_single_reconciliation_sql(),
    ):
        admin_readonly.assert_readonly_sql(sql)


def test_build_payout_status_summary_chunked_with_active_reconciliation() -> None:
    chunks = [_chunk(i) for i in range(9)]
    summary = status_summary.build_payout_status_summary(
        execution=_execution(notes="chunked_executor_v0 chunk_amount=25.000000000000"),
        execution_rows=[_execution_row()],
        chunks=chunks,
        active_chunked_reconciliation=_active_chunked_reconciliation(),
        single_reconciliation_row=None,
    )
    assert summary["production_execution_id"] == _EXECUTION_ID
    assert summary["payout_plan_id"] == _PLAN_ID
    assert summary["execution_status"] == "confirmed"
    assert summary["is_chunked_execution"] is True
    assert summary["chunk_summary"]["chunk_count"] == 9
    assert summary["chunk_summary"]["chunk_status_counts"] == {"confirmed": 9}
    recon = summary["active_reconciliation"]
    assert recon is not None
    assert recon["kind"] == "chunked"
    assert recon["reconciliation_id"] == 2
    assert recon["matched"] is True
    assert recon["is_active"] is True
    assert recon["expected_chunk_count"] == 9
    assert recon["source_chunk_count"] == 9
    assert recon["receiver_chunk_count"] == 9


def test_build_payout_status_summary_single_send() -> None:
    summary = status_summary.build_payout_status_summary(
        execution=_execution(notes=None, txid="tx-single"),
        execution_rows=[_execution_row()],
        chunks=[],
        active_chunked_reconciliation=None,
        single_reconciliation_row={
            "id": 1,
            "production_execution_id": _EXECUTION_ID,
            "payout_plan_id": _PLAN_ID,
            "source_wallet_name": "wallet",
            "txid": "tx-single",
            "reconciliation_status": "matched",
            "expected_amount": Decimal("223.125000000000"),
            "expected_address": _ADDRESS,
            "source_confirmations": 10,
            "source_fee": Decimal("0.001"),
            "source_amount": Decimal("223.125000000000"),
            "receiver_confirmations": 10,
            "receiver_amount": Decimal("223.125000000000"),
            "receiver_category": "receive",
            "receiver_address": _ADDRESS,
            "matched": True,
            "mismatch_reason": None,
            "source_wallet_evidence": None,
            "receiver_wallet_evidence": None,
            "notes": None,
            "created_at": _NOW,
            "updated_at": _NOW,
        },
    )
    assert summary["is_chunked_execution"] is False
    assert summary["chunk_summary"] is None
    recon = summary["active_reconciliation"]
    assert recon is not None
    assert recon["kind"] == "single"
    assert recon["matched"] is True
    assert recon["is_active"] is True


def test_build_payout_status_summary_without_reconciliation() -> None:
    summary = status_summary.build_payout_status_summary(
        execution=_execution(notes=None, txid=None),
        execution_rows=[_execution_row()],
        chunks=[],
        active_chunked_reconciliation=None,
        single_reconciliation_row=None,
    )
    assert summary["active_reconciliation"] is None


def test_status_summary_script_is_read_only_db() -> None:
    script = Path(
        AZPOOL_ROOT / "payouts/scripts/sc_node_payout_status_summary.py"
    ).read_text(encoding="utf-8")
    assert "set_read_only(True)" in script
    assert "INSERT" not in script
    assert "UPDATE" not in script
