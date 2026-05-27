from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from payouts.collector.app import admin_readonly
from payouts.collector.app import sc_node_payout_production_chunked_executor as chunked_executor
from payouts.collector.app import sc_node_payout_production_executor as production_executor
from payouts.collector.app import sc_node_payout_production_preflight as production_preflight
from payouts.collector.app import sc_node_payout_status_summary as status_summary

VERDICT_CLOSED = "CLOSED"
VERDICT_READY = "READY"
VERDICT_NEEDS_EVIDENCE = "NEEDS_EVIDENCE"
VERDICT_HALT = "HALT"

VERDICT_EXIT_CODES: dict[str, int] = {
    VERDICT_CLOSED: 0,
    VERDICT_READY: 0,
    VERDICT_NEEDS_EVIDENCE: 2,
    VERDICT_HALT: 3,
}

_EXECUTION_HALT_STATUSES = frozenset(
    {
        production_executor.EXECUTION_STATUS_REFUSED,
        production_executor.EXECUTION_STATUS_VOID,
        chunked_executor.EXECUTION_STATUS_PARTIAL_SENT,
    }
)

_CHUNK_HALT_STATUSES = frozenset({chunked_executor.CHUNK_STATUS_REFUSED})


def build_active_chunked_reconciliation_count_sql() -> str:
    sql = """
SELECT COUNT(*)::bigint AS active_count
FROM sc_node_chunked_payout_reconciliations
WHERE production_execution_id = %(production_execution_id)s
  AND superseded_at IS NULL
""".strip()
    admin_readonly.assert_readonly_sql(sql)
    return sql


def build_cycle_readiness_preflight_sql(production_preflight_id: int) -> str:
    sql = production_preflight.build_production_preflight_details_sql(production_preflight_id)
    admin_readonly.assert_readonly_sql(sql)
    return sql


def verdict_exit_code(verdict: str) -> int:
    try:
        return VERDICT_EXIT_CODES[verdict]
    except KeyError as exc:
        raise ValueError(f"unknown verdict: {verdict}") from exc


def _decimal_equal(left: object, right: object) -> bool:
    return Decimal(str(left or "0")) == Decimal(str(right or "0"))


def _chunk_has_txid(chunk: Mapping[str, Any]) -> bool:
    return bool(str(chunk.get("txid") or "").strip())


def _supersede_linkage_inconsistent(reconciliation: Mapping[str, Any]) -> str | None:
    if reconciliation.get("superseded_at") is not None:
        return "active reconciliation row has superseded_at set"
    if reconciliation.get("superseded_by_reconciliation_id") is not None:
        return "active reconciliation row has superseded_by_reconciliation_id set"
    return None


def _chunked_counts_aligned(
    *,
    chunk_count: int,
    reconciliation: Mapping[str, Any],
) -> bool:
    expected = int(reconciliation.get("expected_chunk_count") or 0)
    source = int(reconciliation.get("source_chunk_count") or 0)
    receiver = reconciliation.get("receiver_chunk_count")
    if expected != chunk_count:
        return False
    if source != expected:
        return False
    if receiver is None:
        return False
    return int(receiver) == expected


def _chunked_totals_aligned(reconciliation: Mapping[str, Any]) -> bool:
    expected = reconciliation.get("expected_amount_total")
    source = reconciliation.get("source_amount_total")
    receiver = reconciliation.get("receiver_amount_total")
    if expected is None or source is None or receiver is None:
        return False
    return _decimal_equal(expected, source) and _decimal_equal(source, receiver)


def _preflight_ready(preflight: Mapping[str, Any] | None) -> bool:
    if preflight is None:
        return False
    if str(preflight.get("preflight_status")) != production_preflight.PREFLIGHT_STATUS_PASSED:
        return False
    return bool(preflight.get("execution_allowed"))


def _evaluate_chunked_execution_integrity(
    *,
    execution_status: str,
    chunks: list[Mapping[str, Any]],
    halt_reasons: list[str],
    missing_evidence: list[str],
) -> None:
    if not chunks:
        if execution_status in {
            production_executor.EXECUTION_STATUS_SENT,
            production_executor.EXECUTION_STATUS_CONFIRMED,
            chunked_executor.EXECUTION_STATUS_PARTIAL_SENT,
        }:
            halt_reasons.append("chunked execution has no chunk rows")
        return

    refused = sum(
        1 for chunk in chunks if str(chunk.get("chunk_status")) in _CHUNK_HALT_STATUSES
    )
    if refused:
        halt_reasons.append(f"{refused} chunk row(s) have refused status")

    missing_txids = [
        chunk
        for chunk in chunks
        if str(chunk.get("chunk_status"))
        in {chunked_executor.CHUNK_STATUS_SENT, chunked_executor.CHUNK_STATUS_CONFIRMED}
        and not _chunk_has_txid(chunk)
    ]
    if missing_txids:
        halt_reasons.append(f"{len(missing_txids)} sent/confirmed chunk(s) missing txid")

    if execution_status == production_executor.EXECUTION_STATUS_CONFIRMED:
        non_confirmed = [
            chunk
            for chunk in chunks
            if str(chunk.get("chunk_status")) != chunked_executor.CHUNK_STATUS_CONFIRMED
        ]
        if non_confirmed:
            halt_reasons.append(
                f"{len(non_confirmed)} chunk row(s) are not confirmed for confirmed execution"
            )
        if missing_txids:
            missing_evidence.append("confirmed chunked execution has chunks without txid")


def _evaluate_closed_chunked(
    *,
    execution_status: str,
    chunks: list[Mapping[str, Any]],
    reconciliation: Mapping[str, Any],
) -> bool:
    if execution_status != production_executor.EXECUTION_STATUS_CONFIRMED:
        return False
    if not bool(reconciliation.get("matched")):
        return False
    if str(reconciliation.get("reconciliation_status")) != "matched":
        return False
    if not bool(reconciliation.get("is_active", True)):
        return False
    if not _chunked_counts_aligned(chunk_count=len(chunks), reconciliation=reconciliation):
        return False
    if not _chunked_totals_aligned(reconciliation):
        return False
    return True


def _evaluate_closed_single(
    *,
    execution_status: str,
    reconciliation: Mapping[str, Any],
) -> bool:
    if execution_status != production_executor.EXECUTION_STATUS_CONFIRMED:
        return False
    if not bool(reconciliation.get("matched")):
        return False
    if str(reconciliation.get("reconciliation_status")) != "matched":
        return False
    return True


def _evaluate_needs_evidence_chunked(reconciliation: Mapping[str, Any] | None) -> list[str]:
    reasons: list[str] = []
    if reconciliation is None:
        reasons.append("no active chunked reconciliation recorded")
        return reasons
    receiver_count = reconciliation.get("receiver_chunk_count")
    expected_count = int(reconciliation.get("expected_chunk_count") or 0)
    if receiver_count is None:
        reasons.append("receiver chunk evidence not recorded on active reconciliation")
    elif int(receiver_count) < expected_count:
        reasons.append(
            "receiver chunk count is below expected chunk count on active reconciliation"
        )
    status = str(reconciliation.get("reconciliation_status") or "")
    if status == "source_only":
        reasons.append("active reconciliation is source_only (receiver evidence missing)")
    if reconciliation.get("receiver_amount_total") is None:
        reasons.append("receiver amount total missing on active reconciliation")
    return reasons


def _evaluate_needs_evidence_single(reconciliation: Mapping[str, Any] | None) -> list[str]:
    reasons: list[str] = []
    if reconciliation is None:
        reasons.append("no reconciliation recorded for execution txid")
        return reasons
    if reconciliation.get("receiver_amount") is None:
        reasons.append("receiver amount missing on reconciliation")
    if reconciliation.get("receiver_address") is None:
        reasons.append("receiver address missing on reconciliation")
    status = str(reconciliation.get("reconciliation_status") or "")
    if status == "draft":
        reasons.append("reconciliation status is draft (receiver evidence missing)")
    return reasons


def evaluate_payout_cycle_readiness(
    *,
    summary: Mapping[str, Any],
    active_chunked_reconciliation_count: int,
    preflight: Mapping[str, Any] | None,
) -> dict[str, Any]:
    halt_reasons: list[str] = []
    missing_evidence: list[str] = []

    execution_status = str(summary.get("execution_status") or "")
    is_chunked = bool(summary.get("is_chunked_execution"))
    chunks = []
    chunk_summary = summary.get("chunk_summary")
    if isinstance(chunk_summary, Mapping):
        raw_chunks = chunk_summary.get("chunks")
        if isinstance(raw_chunks, list):
            chunks = [chunk for chunk in raw_chunks if isinstance(chunk, Mapping)]

    reconciliation = summary.get("active_reconciliation")
    recon_mapping = reconciliation if isinstance(reconciliation, Mapping) else None

    if execution_status in _EXECUTION_HALT_STATUSES:
        halt_reasons.append(f"execution status is {execution_status}")

    if active_chunked_reconciliation_count > 1:
        halt_reasons.append(
            f"multiple active chunked reconciliations detected ({active_chunked_reconciliation_count})"
        )

    if is_chunked:
        _evaluate_chunked_execution_integrity(
            execution_status=execution_status,
            chunks=chunks,
            halt_reasons=halt_reasons,
            missing_evidence=missing_evidence,
        )

    if recon_mapping is not None and str(recon_mapping.get("kind")) == "chunked":
        supersede_issue = _supersede_linkage_inconsistent(recon_mapping)
        if supersede_issue is not None:
            halt_reasons.append(supersede_issue)
        if not bool(recon_mapping.get("is_active", True)):
            halt_reasons.append("chunked reconciliation is not active")
        if recon_mapping.get("matched") is False:
            refusal = recon_mapping.get("refusal_reason") or recon_mapping.get(
                "reconciliation_status"
            )
            halt_reasons.append(
                f"active chunked reconciliation is not matched ({refusal})"
            )

    if (
        recon_mapping is not None
        and str(recon_mapping.get("kind")) == "single"
        and recon_mapping.get("matched") is False
    ):
        mismatch = recon_mapping.get("mismatch_reason") or recon_mapping.get(
            "reconciliation_status"
        )
        halt_reasons.append(f"reconciliation is not matched ({mismatch})")

    if (
        not is_chunked
        and execution_status == production_executor.EXECUTION_STATUS_SENT
        and not str(summary.get("execution_txid") or "").strip()
    ):
        halt_reasons.append("sent single-send execution is missing txid")

    verdict = VERDICT_HALT
    if halt_reasons:
        verdict = VERDICT_HALT
    elif is_chunked and recon_mapping is not None and str(recon_mapping.get("kind")) == "chunked":
        if _evaluate_closed_chunked(
            execution_status=execution_status,
            chunks=chunks,
            reconciliation=recon_mapping,
        ):
            verdict = VERDICT_CLOSED
        elif (
            execution_status == production_executor.EXECUTION_STATUS_CONFIRMED
            and bool(recon_mapping.get("matched"))
        ):
            halt_reasons.append("matched reconciliation fails closeout alignment checks")
            verdict = VERDICT_HALT
        else:
            missing_evidence.extend(_evaluate_needs_evidence_chunked(recon_mapping))
            verdict = VERDICT_NEEDS_EVIDENCE
    elif (
        not is_chunked
        and recon_mapping is not None
        and str(recon_mapping.get("kind")) == "single"
    ):
        if _evaluate_closed_single(
            execution_status=execution_status,
            reconciliation=recon_mapping,
        ):
            verdict = VERDICT_CLOSED
        elif (
            execution_status == production_executor.EXECUTION_STATUS_CONFIRMED
            and bool(recon_mapping.get("matched"))
        ):
            halt_reasons.append("matched reconciliation fails closeout alignment checks")
            verdict = VERDICT_HALT
        else:
            missing_evidence.extend(_evaluate_needs_evidence_single(recon_mapping))
            verdict = VERDICT_NEEDS_EVIDENCE
    elif execution_status == production_executor.EXECUTION_STATUS_DRAFT:
        if _preflight_ready(preflight):
            verdict = VERDICT_READY
        else:
            halt_reasons.append("draft execution preflight is missing or not execution_allowed")
            verdict = VERDICT_HALT
    elif execution_status == production_executor.EXECUTION_STATUS_SENT:
        if is_chunked:
            if chunks and all(
                str(chunk.get("chunk_status")) == chunked_executor.CHUNK_STATUS_SENT
                and _chunk_has_txid(chunk)
                for chunk in chunks
            ):
                verdict = VERDICT_READY
            else:
                halt_reasons.append("chunked execution is sent but chunk txids/status are incomplete")
                verdict = VERDICT_HALT
        elif str(summary.get("execution_txid") or "").strip():
            verdict = VERDICT_READY
        else:
            halt_reasons.append("sent single-send execution is missing txid")
            verdict = VERDICT_HALT
    elif execution_status == production_executor.EXECUTION_STATUS_CONFIRMED:
        if is_chunked:
            missing_evidence.extend(_evaluate_needs_evidence_chunked(recon_mapping))
        else:
            missing_evidence.extend(_evaluate_needs_evidence_single(recon_mapping))
        verdict = VERDICT_NEEDS_EVIDENCE
    else:
        halt_reasons.append(f"ambiguous cycle state for execution status {execution_status}")
        verdict = VERDICT_HALT

    if verdict == VERDICT_NEEDS_EVIDENCE and not missing_evidence:
        missing_evidence.append("reconciliation or wallet evidence is incomplete")

    report: dict[str, Any] = {
        "command": "payout-cycle-readiness",
        "verdict": verdict,
        "exit_code": verdict_exit_code(verdict),
        "production_execution_id": summary.get("production_execution_id"),
        "payout_plan_id": summary.get("payout_plan_id"),
        "production_preflight_id": summary.get("production_preflight_id"),
        "execution_status": execution_status,
        "execution_txid": summary.get("execution_txid"),
        "is_chunked_execution": is_chunked,
        "chunk_summary": chunk_summary,
        "active_reconciliation": recon_mapping,
        "active_chunked_reconciliation_count": int(active_chunked_reconciliation_count),
        "missing_evidence_reasons": missing_evidence,
        "halt_reasons": halt_reasons,
        "preflight_execution_allowed": (
            bool(preflight.get("execution_allowed")) if preflight is not None else None
        ),
        "preflight_status": (
            preflight.get("preflight_status") if preflight is not None else None
        ),
    }
    return report


def format_readiness_text(report: Mapping[str, Any]) -> str:
    lines = [
        "Payout cycle readiness",
        "=====================",
        f"Production execution id: {report.get('production_execution_id')}",
        f"Payout plan id: {report.get('payout_plan_id')}",
        f"Execution status: {report.get('execution_status')}",
        f"Chunked execution: {report.get('is_chunked_execution')}",
    ]

    execution_txid = report.get("execution_txid")
    if execution_txid:
        lines.append(f"Execution txid: {execution_txid}")

    chunk_summary = report.get("chunk_summary")
    if isinstance(chunk_summary, Mapping):
        lines.append(f"Chunk count: {chunk_summary.get('chunk_count')}")
        counts = chunk_summary.get("chunk_status_counts")
        if counts:
            lines.append(f"Chunk status counts: {counts}")

    recon = report.get("active_reconciliation")
    if isinstance(recon, Mapping):
        lines.append(f"Active reconciliation id: {recon.get('reconciliation_id')}")
        lines.append(f"Reconciliation kind: {recon.get('kind')}")
        lines.append(f"Matched: {recon.get('matched')}")
        if recon.get("kind") == "chunked":
            lines.append(
                "Chunk counts (expected/source/receiver): "
                f"{recon.get('expected_chunk_count')}/"
                f"{recon.get('source_chunk_count')}/"
                f"{recon.get('receiver_chunk_count')}"
            )
            lines.append(
                "Totals (expected/source/receiver): "
                f"{recon.get('expected_amount_total')}/"
                f"{recon.get('source_amount_total')}/"
                f"{recon.get('receiver_amount_total')}"
            )
            if recon.get("supersedes_reconciliation_id") is not None:
                lines.append(
                    f"Supersedes reconciliation id: {recon.get('supersedes_reconciliation_id')}"
                )
            if recon.get("superseded_by_reconciliation_id") is not None:
                lines.append(
                    "Superseded by reconciliation id: "
                    f"{recon.get('superseded_by_reconciliation_id')}"
                )
        elif recon.get("kind") == "single":
            lines.append(f"Reconciliation txid: {recon.get('txid')}")
            lines.append(
                "Amounts (expected/source/receiver): "
                f"{recon.get('expected_amount')}/"
                f"{recon.get('source_amount')}/"
                f"{recon.get('receiver_amount')}"
            )
    else:
        lines.append("Active reconciliation: none")

    active_count = report.get("active_chunked_reconciliation_count")
    if active_count is not None:
        lines.append(f"Active chunked reconciliation rows: {active_count}")

    missing = report.get("missing_evidence_reasons") or []
    if missing:
        lines.append("")
        lines.append("Missing evidence:")
        for reason in missing:
            lines.append(f"- {reason}")

    halt = report.get("halt_reasons") or []
    if halt:
        lines.append("")
        lines.append("Halt reasons:")
        for reason in halt:
            lines.append(f"- {reason}")

    lines.append("")
    lines.append(f"Verdict: {report.get('verdict')}")
    return "\n".join(lines) + "\n"
