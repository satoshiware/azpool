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
from payouts.collector.app import sc_node_payout_cycle_readiness as readiness
from payouts.collector.app import sc_node_payout_status_summary as status_summary


_EXECUTION_ID = 3
_PLAN_ID = 2
_PREFLIGHT_ID = 2
_ADDRESS = "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv"
_PLANNED = Decimal("223.125000000000")
_NOW = datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc)


def _execution(
    *,
    status: str = "confirmed",
    notes: str | None = "chunked_executor_v0 chunk_amount=25.000000000000",
    txid: str | None = "tx-main",
) -> dict[str, object]:
    return {
        "id": _EXECUTION_ID,
        "payout_plan_id": _PLAN_ID,
        "production_preflight_id": _PREFLIGHT_ID,
        "source_wallet_name": "wallet",
        "status": status,
        "planned_amount_total": _PLANNED,
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
        "payout_amount": _PLANNED,
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


def _active_chunked_reconciliation(**overrides: object) -> dict[str, object]:
    row = {
        "id": 2,
        "production_execution_id": _EXECUTION_ID,
        "payout_plan_id": _PLAN_ID,
        "sc_node_id": "sc-2",
        "payout_address": _ADDRESS,
        "expected_chunk_count": 9,
        "source_chunk_count": 9,
        "receiver_chunk_count": 9,
        "expected_amount_total": _PLANNED,
        "source_amount_total": _PLANNED,
        "source_fee_total": Decimal("0.001"),
        "receiver_amount_total": _PLANNED,
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
    row.update(overrides)
    return row


def _summary(
    *,
    execution: dict[str, object],
    chunks: list[dict[str, object]] | None = None,
    active_reconciliation: dict[str, object] | None = None,
    single_reconciliation: dict[str, object] | None = None,
) -> dict[str, object]:
    return status_summary.build_payout_status_summary(
        execution=execution,
        execution_rows=[_execution_row()],
        chunks=chunks or [],
        active_chunked_reconciliation=active_reconciliation,
        single_reconciliation_row=single_reconciliation,
    )


def _evaluate(**kwargs: object) -> dict[str, object]:
    return readiness.evaluate_payout_cycle_readiness(
        active_chunked_reconciliation_count=kwargs.pop("active_chunked_reconciliation_count", 1),
        preflight=kwargs.pop("preflight", None),
        **kwargs,
    )


def test_readiness_sql_builders_are_select_only() -> None:
    admin_readonly.assert_readonly_sql(readiness.build_active_chunked_reconciliation_count_sql())
    admin_readonly.assert_readonly_sql(readiness.build_cycle_readiness_preflight_sql(_PREFLIGHT_ID))


def test_verdict_closed_chunked_cycle() -> None:
    chunks = [_chunk(i) for i in range(9)]
    report = _evaluate(
        summary=_summary(
            execution=_execution(),
            chunks=chunks,
            active_reconciliation=_active_chunked_reconciliation(),
        ),
    )
    assert report["verdict"] == readiness.VERDICT_CLOSED
    assert report["exit_code"] == 0


def test_verdict_needs_evidence_confirmed_without_reconciliation() -> None:
    report = _evaluate(
        summary=_summary(execution=_execution(), chunks=[_chunk(0)]),
        active_chunked_reconciliation_count=0,
    )
    assert report["verdict"] == readiness.VERDICT_NEEDS_EVIDENCE
    assert report["exit_code"] == 2
    assert report["missing_evidence_reasons"]


def test_verdict_halt_refused_execution() -> None:
    report = _evaluate(
        summary=_summary(
            execution=_execution(status="refused", notes=None, txid=None),
        ),
        active_chunked_reconciliation_count=0,
    )
    assert report["verdict"] == readiness.VERDICT_HALT
    assert report["exit_code"] == 3


def test_verdict_halt_partial_sent() -> None:
    report = _evaluate(
        summary=_summary(
            execution=_execution(status="partial_sent"),
            chunks=[_chunk(0, status="sent"), _chunk(1, status="draft", txid=None)],
        ),
    )
    assert report["verdict"] == readiness.VERDICT_HALT
    assert "partial_sent" in " ".join(report["halt_reasons"])


def test_verdict_halt_multiple_active_reconciliations() -> None:
    report = _evaluate(
        summary=_summary(
            execution=_execution(),
            chunks=[_chunk(0)],
            active_reconciliation=_active_chunked_reconciliation(matched=False),
        ),
        active_chunked_reconciliation_count=2,
    )
    assert report["verdict"] == readiness.VERDICT_HALT


def test_verdict_halt_unmatched_active_reconciliation() -> None:
    report = _evaluate(
        summary=_summary(
            execution=_execution(),
            chunks=[_chunk(i) for i in range(9)],
            active_reconciliation=_active_chunked_reconciliation(
                matched=False,
                reconciliation_status="mismatch",
                receiver_chunk_count=1,
            ),
        ),
    )
    assert report["verdict"] == readiness.VERDICT_HALT


def test_verdict_ready_draft_with_passed_preflight() -> None:
    report = _evaluate(
        summary=_summary(
            execution=_execution(status="draft", notes=None, txid=None),
        ),
        active_chunked_reconciliation_count=0,
        preflight={
            "preflight_status": "passed",
            "execution_allowed": True,
        },
    )
    assert report["verdict"] == readiness.VERDICT_READY
    assert report["exit_code"] == 0


def test_verdict_ready_sent_single_awaiting_confirm() -> None:
    report = _evaluate(
        summary=_summary(
            execution=_execution(status="sent", notes=None, txid="tx-single"),
        ),
        active_chunked_reconciliation_count=0,
    )
    assert report["verdict"] == readiness.VERDICT_READY


def test_verdict_ready_sent_chunked_awaiting_confirm() -> None:
    chunks = [_chunk(i, status="sent") for i in range(9)]
    report = _evaluate(
        summary=_summary(
            execution=_execution(status="sent"),
            chunks=chunks,
        ),
        active_chunked_reconciliation_count=0,
    )
    assert report["verdict"] == readiness.VERDICT_READY


def test_format_readiness_text_includes_verdict() -> None:
    report = _evaluate(
        summary=_summary(
            execution=_execution(),
            chunks=[_chunk(i) for i in range(9)],
            active_reconciliation=_active_chunked_reconciliation(),
        ),
    )
    text = readiness.format_readiness_text(report)
    assert "Verdict: CLOSED" in text
    assert "Production execution id: 3" in text


def test_readiness_script_is_read_only_db() -> None:
    script = Path(AZPOOL_ROOT / "payouts/scripts/sc_node_payout_cycle_readiness.py").read_text(
        encoding="utf-8",
    )
    assert "set_read_only(True)" in script
    assert "subprocess" not in script
