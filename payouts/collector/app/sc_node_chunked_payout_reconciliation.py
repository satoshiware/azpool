from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any, Mapping

from payouts.collector.app import sc_node_payout_planner as planner
from payouts.collector.app import sc_node_payout_production_chunked_executor as chunked_executor
from payouts.collector.app import sc_node_payout_production_executor as production_executor
from payouts.collector.app import sc_node_payout_reconciliation as single_reconciliation

RECONCILIATION_STATUS_MATCHED = "matched"
RECONCILIATION_STATUS_MISMATCH = "mismatch"
RECONCILIATION_STATUS_SOURCE_ONLY = "source_only"

ROW_STATUS_MATCHED = "matched"
ROW_STATUS_MISMATCH = "mismatch"
ROW_STATUS_SOURCE_ONLY = "source_only"

EXECUTION_STATUS_CONFIRMED = production_executor.EXECUTION_STATUS_CONFIRMED
CHUNK_STATUS_CONFIRMED = chunked_executor.CHUNK_STATUS_CONFIRMED

RECEIVER_CATEGORY_RECEIVE = single_reconciliation.RECEIVER_CATEGORY_RECEIVE

_CHUNKED_RECON_INSERT_TABLES = frozenset(
    {
        "sc_node_chunked_payout_reconciliations",
        "sc_node_chunked_payout_reconciliation_chunks",
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
class ChunkReconciliationPreview:
    production_execution_chunk_id: int
    chunk_index: int
    txid: str
    expected_amount: Decimal
    source_amount: Decimal | None
    source_fee: Decimal | None
    source_confirmations: int | None
    source_blockhash: str | None
    receiver_amount: Decimal | None
    receiver_address: str | None
    receiver_confirmations: int | None
    receiver_category: str | None
    row_status: str
    mismatch_reason: str | None


@dataclass(frozen=True)
class ChunkedReconciliationPreview:
    production_execution_id: int
    payout_plan_id: int
    sc_node_id: str
    payout_address: str
    source_wallet_name: str
    expected_chunk_count: int
    source_chunk_count: int
    receiver_chunk_count: int | None
    expected_amount_total: Decimal
    source_amount_total: Decimal
    source_fee_total: Decimal | None
    receiver_amount_total: Decimal | None
    reconciliation_status: str
    matched: bool
    mismatch_reason: str | None
    source_wallet_evidence: dict[str, Any]
    receiver_wallet_evidence: dict[str, Any] | None
    chunks: tuple[ChunkReconciliationPreview, ...]


def assert_no_wallet_send_keywords(text: str) -> None:
    if _WALLET_SEND_KEYWORDS.search(text):
        raise ValueError("text must not contain wallet send or signing keywords")


def _assert_readonly_sql(sql: str) -> None:
    if _READONLY_SQL_FORBIDDEN.search(sql):
        raise ValueError("SQL must be read-only SELECT only")


def _assert_chunked_recon_insert_sql(sql: str) -> None:
    assert_no_wallet_send_keywords(sql)
    lowered = sql.lower()
    if "insert into" not in lowered:
        raise ValueError("chunked reconciliation SQL must INSERT")
    for token in re.findall(r"insert\s+into\s+([a-z0-9_]+)", lowered):
        if token not in _CHUNKED_RECON_INSERT_TABLES:
            raise ValueError(f"chunked reconciliation SQL must not target table: {token}")


def _quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000000000001"), rounding=ROUND_DOWN)


def _to_decimal(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def normalize_source_wallet_name(value: str) -> str:
    return production_executor.normalize_source_wallet_name(value)


def parse_source_gettransaction(
    payload: Mapping[str, Any],
    txid: str,
) -> single_reconciliation.SourceTransactionEvidence:
    return single_reconciliation.parse_source_gettransaction(payload, txid)


def parse_receiver_transactions_by_txid(
    rows: list[dict[str, Any]],
) -> dict[str, single_reconciliation.ReceiverTransactionEvidence]:
    by_txid: dict[str, single_reconciliation.ReceiverTransactionEvidence] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        txid = str(row.get("txid", "")).strip()
        if not txid or txid in by_txid:
            continue
        evidence = single_reconciliation.parse_receiver_transactions_json([dict(row)], txid)
        if evidence is not None:
            by_txid[txid] = evidence
    return by_txid


def sanitize_wallet_evidence(
    evidence: object,
    *,
    include_raw: bool = False,
) -> dict[str, Any] | list[Any] | None:
    if evidence is None:
        return None
    if isinstance(evidence, Mapping):
        if "by_txid" in evidence and isinstance(evidence["by_txid"], Mapping):
            sanitized: dict[str, Any] = {}
            for txid, payload in evidence["by_txid"].items():
                cleaned = single_reconciliation.sanitize_source_wallet_evidence(
                    payload,
                    include_raw=include_raw,
                )
                if cleaned is not None:
                    sanitized[str(txid)] = cleaned
            return {"by_txid": sanitized}
        return single_reconciliation.sanitize_source_wallet_evidence(
            evidence,
            include_raw=include_raw,
        ) or {}
    if isinstance(evidence, list):
        return [
            dict(item)
            if include_raw
            else {
                k: v
                for k, v in dict(item).items()
                if k != "hex"
            }
            for item in evidence
            if isinstance(item, Mapping)
        ]
    return None


def build_confirmed_chunked_execution_sql(production_execution_id: int) -> str:
    safe_id = int(production_execution_id)
    sql = f"""
SELECT
  id,
  payout_plan_id,
  production_preflight_id,
  source_wallet_name,
  status,
  planned_amount_total,
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


def build_confirmed_execution_chunks_sql(production_execution_id: int) -> str:
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
  AND chunk_status = '{CHUNK_STATUS_CONFIRMED}'
ORDER BY chunk_index
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_insert_chunked_reconciliation_sql() -> str:
    sql = """
INSERT INTO sc_node_chunked_payout_reconciliations (
  production_execution_id,
  payout_plan_id,
  sc_node_id,
  payout_address,
  expected_chunk_count,
  source_chunk_count,
  receiver_chunk_count,
  expected_amount_total,
  source_amount_total,
  source_fee_total,
  receiver_amount_total,
  reconciliation_status,
  matched,
  refusal_reason,
  source_wallet_name,
  source_wallet_evidence,
  receiver_wallet_evidence
) VALUES (
  %(production_execution_id)s,
  %(payout_plan_id)s,
  %(sc_node_id)s,
  %(payout_address)s,
  %(expected_chunk_count)s,
  %(source_chunk_count)s,
  %(receiver_chunk_count)s,
  %(expected_amount_total)s,
  %(source_amount_total)s,
  %(source_fee_total)s,
  %(receiver_amount_total)s,
  %(reconciliation_status)s,
  %(matched)s,
  %(refusal_reason)s,
  %(source_wallet_name)s,
  %(source_wallet_evidence)s,
  %(receiver_wallet_evidence)s
)
RETURNING id
""".strip()
    _assert_chunked_recon_insert_sql(sql)
    return sql


def build_insert_chunked_reconciliation_chunk_sql() -> str:
    sql = """
INSERT INTO sc_node_chunked_payout_reconciliation_chunks (
  reconciliation_id,
  production_execution_chunk_id,
  chunk_index,
  txid,
  expected_amount,
  source_amount,
  source_fee,
  source_confirmations,
  source_blockhash,
  receiver_amount,
  receiver_address,
  receiver_confirmations,
  receiver_category,
  row_status,
  refusal_reason
) VALUES (
  %(reconciliation_id)s,
  %(production_execution_chunk_id)s,
  %(chunk_index)s,
  %(txid)s,
  %(expected_amount)s,
  %(source_amount)s,
  %(source_fee)s,
  %(source_confirmations)s,
  %(source_blockhash)s,
  %(receiver_amount)s,
  %(receiver_address)s,
  %(receiver_confirmations)s,
  %(receiver_category)s,
  %(row_status)s,
  %(refusal_reason)s
)
""".strip()
    _assert_chunked_recon_insert_sql(sql)
    return sql


def build_chunked_reconciliations_sql() -> str:
    sql = """
SELECT
  id,
  production_execution_id,
  payout_plan_id,
  sc_node_id,
  payout_address,
  expected_chunk_count,
  source_chunk_count,
  receiver_chunk_count,
  expected_amount_total,
  source_amount_total,
  source_fee_total,
  receiver_amount_total,
  reconciliation_status,
  matched,
  refusal_reason,
  source_wallet_name,
  source_wallet_evidence,
  receiver_wallet_evidence,
  created_at,
  updated_at
FROM sc_node_chunked_payout_reconciliations
ORDER BY created_at DESC, id DESC
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_chunked_reconciliation_details_sql(reconciliation_id: int) -> str:
    safe_id = int(reconciliation_id)
    sql = f"""
SELECT
  id,
  production_execution_id,
  payout_plan_id,
  sc_node_id,
  payout_address,
  expected_chunk_count,
  source_chunk_count,
  receiver_chunk_count,
  expected_amount_total,
  source_amount_total,
  source_fee_total,
  receiver_amount_total,
  reconciliation_status,
  matched,
  refusal_reason,
  source_wallet_name,
  source_wallet_evidence,
  receiver_wallet_evidence,
  created_at,
  updated_at
FROM sc_node_chunked_payout_reconciliations
WHERE id = {safe_id}
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_chunked_reconciliation_chunks_sql(reconciliation_id: int) -> str:
    safe_id = int(reconciliation_id)
    sql = f"""
SELECT
  id,
  reconciliation_id,
  production_execution_chunk_id,
  chunk_index,
  txid,
  expected_amount,
  source_amount,
  source_fee,
  source_confirmations,
  source_blockhash,
  receiver_amount,
  receiver_address,
  receiver_confirmations,
  receiver_category,
  row_status,
  refusal_reason,
  created_at,
  updated_at
FROM sc_node_chunked_payout_reconciliation_chunks
WHERE reconciliation_id = {safe_id}
ORDER BY chunk_index
""".strip()
    _assert_readonly_sql(sql)
    return sql


def build_chunked_reconciliation_by_execution_sql() -> str:
    sql = """
SELECT
  id,
  production_execution_id,
  payout_plan_id,
  sc_node_id,
  payout_address,
  expected_chunk_count,
  source_chunk_count,
  receiver_chunk_count,
  expected_amount_total,
  source_amount_total,
  source_fee_total,
  receiver_amount_total,
  reconciliation_status,
  matched,
  refusal_reason,
  source_wallet_name,
  created_at,
  updated_at
FROM sc_node_chunked_payout_reconciliations
WHERE production_execution_id = %(production_execution_id)s
""".strip()
    _assert_readonly_sql(sql)
    return sql


def _compare_chunk(
    *,
    chunk: Mapping[str, Any],
    expected_address: str,
    source: single_reconciliation.SourceTransactionEvidence | None,
    receiver: single_reconciliation.ReceiverTransactionEvidence | None,
    receiver_provided: bool,
) -> ChunkReconciliationPreview:
    chunk_id = int(chunk["id"])
    chunk_index = int(chunk["chunk_index"])
    txid = str(chunk["txid"]).strip()
    expected_amount = _quantize_amount(_to_decimal(chunk.get("chunk_amount")))
    reasons: list[str] = []

    source_amount: Decimal | None = None
    source_fee: Decimal | None = None
    source_confirmations: int | None = None
    source_blockhash: str | None = None

    if not txid:
        reasons.append("chunk txid missing")
    if source is None:
        reasons.append("source gettransaction missing")
    else:
        source_amount = source.amount
        source_fee = source.fee
        source_confirmations = source.confirmations
        source_blockhash = str(source.raw.get("blockhash") or "") or None
        if source.txid != txid:
            reasons.append("source txid mismatch")
        if source.confirmations < 1:
            reasons.append("source confirmations pending")
        if source_amount is None or source_amount != expected_amount:
            reasons.append("source amount mismatch")

    receiver_amount: Decimal | None = None
    receiver_address: str | None = None
    receiver_confirmations: int | None = None
    receiver_category: str | None = None

    if receiver_provided:
        if receiver is None:
            reasons.append("receiver txid missing")
        else:
            receiver_amount = receiver.amount
            receiver_address = receiver.address
            receiver_confirmations = receiver.confirmations
            receiver_category = receiver.category
            if receiver.txid != txid:
                reasons.append("receiver txid mismatch")
            if receiver.category != RECEIVER_CATEGORY_RECEIVE:
                reasons.append("receiver category must be receive")
            if receiver.address != expected_address:
                reasons.append("receiver address mismatch")
            if receiver_amount != expected_amount:
                reasons.append("receiver amount mismatch")

    if reasons:
        row_status = ROW_STATUS_MISMATCH
    elif not receiver_provided:
        row_status = ROW_STATUS_SOURCE_ONLY
    else:
        row_status = ROW_STATUS_MATCHED

    return ChunkReconciliationPreview(
        production_execution_chunk_id=chunk_id,
        chunk_index=chunk_index,
        txid=txid,
        expected_amount=expected_amount,
        source_amount=source_amount,
        source_fee=source_fee,
        source_confirmations=source_confirmations,
        source_blockhash=source_blockhash,
        receiver_amount=receiver_amount,
        receiver_address=receiver_address,
        receiver_confirmations=receiver_confirmations,
        receiver_category=receiver_category,
        row_status=row_status,
        mismatch_reason="; ".join(reasons) if reasons else None,
    )


def build_chunked_reconciliation_preview(
    *,
    execution: Mapping[str, Any],
    chunks: list[Mapping[str, Any]],
    source_wallet_name: str,
    source_by_txid: dict[str, single_reconciliation.SourceTransactionEvidence],
    receiver_rows: list[dict[str, Any]] | None,
) -> ChunkedReconciliationPreview:
    if not chunks:
        raise ValueError("confirmed chunk rows required for chunked reconciliation")

    production_execution_id = int(execution["id"])
    payout_plan_id = int(execution["payout_plan_id"])
    planned_total = _quantize_amount(_to_decimal(execution.get("planned_amount_total")))

    first = chunks[0]
    sc_node_id = str(first["sc_node_id"])
    payout_address = str(first["payout_address"]).strip()

    receiver_by_txid = (
        parse_receiver_transactions_by_txid(receiver_rows) if receiver_rows else {}
    )
    receiver_provided = receiver_rows is not None

    chunk_previews: list[ChunkReconciliationPreview] = []
    source_evidence_store: dict[str, Any] = {}
    source_amount_total = Decimal("0")
    source_fee_total = Decimal("0")
    fee_seen = False
    source_chunk_count = 0
    receiver_amount_total = Decimal("0") if receiver_provided else None
    receiver_chunk_count = 0 if receiver_provided else None

    for chunk in chunks:
        txid = str(chunk["txid"]).strip()
        source = source_by_txid.get(txid)
        receiver = receiver_by_txid.get(txid) if receiver_provided else None
        preview_row = _compare_chunk(
            chunk=chunk,
            expected_address=payout_address,
            source=source,
            receiver=receiver,
            receiver_provided=receiver_provided,
        )
        chunk_previews.append(preview_row)

        if source is not None:
            source_chunk_count += 1
            source_evidence_store[txid] = source.raw
            if source.amount is not None:
                source_amount_total = _quantize_amount(
                    source_amount_total + source.amount
                )
            if source.fee is not None:
                source_fee_total = _quantize_amount(source_fee_total + source.fee)
                fee_seen = True

        if receiver_provided and receiver is not None:
            assert receiver_chunk_count is not None
            assert receiver_amount_total is not None
            receiver_chunk_count += 1
            receiver_amount_total = _quantize_amount(
                receiver_amount_total + receiver.amount
            )

    expected_amount_total = _quantize_amount(
        sum((p.expected_amount for p in chunk_previews), Decimal("0"))
    )

    header_reasons: list[str] = []
    if expected_amount_total != planned_total:
        header_reasons.append(
            "expected_amount_total does not match planned_amount_total"
        )
    if source_amount_total != expected_amount_total:
        header_reasons.append("source_amount_total mismatch")
    if receiver_provided and receiver_amount_total != expected_amount_total:
        header_reasons.append("receiver_amount_total mismatch")
    if source_chunk_count != len(chunks):
        header_reasons.append("not all chunks have valid source evidence")
    if receiver_provided and receiver_chunk_count != len(chunks):
        header_reasons.append("not all chunks have receiver evidence")

    row_mismatch = any(p.row_status == ROW_STATUS_MISMATCH for p in chunk_previews)
    if row_mismatch:
        header_reasons.append("one or more chunk rows mismatched")

    if header_reasons:
        reconciliation_status = RECONCILIATION_STATUS_MISMATCH
        matched = False
    elif not receiver_provided:
        reconciliation_status = RECONCILIATION_STATUS_SOURCE_ONLY
        matched = False
    else:
        reconciliation_status = RECONCILIATION_STATUS_MATCHED
        matched = True

    receiver_evidence: dict[str, Any] | None = None
    if receiver_rows is not None:
        receiver_evidence = {"transactions": [dict(row) for row in receiver_rows]}

    return ChunkedReconciliationPreview(
        production_execution_id=production_execution_id,
        payout_plan_id=payout_plan_id,
        sc_node_id=sc_node_id,
        payout_address=payout_address,
        source_wallet_name=source_wallet_name,
        expected_chunk_count=len(chunks),
        source_chunk_count=source_chunk_count,
        receiver_chunk_count=receiver_chunk_count,
        expected_amount_total=expected_amount_total,
        source_amount_total=source_amount_total,
        source_fee_total=source_fee_total if fee_seen else None,
        receiver_amount_total=receiver_amount_total,
        reconciliation_status=reconciliation_status,
        matched=matched,
        mismatch_reason="; ".join(header_reasons) if header_reasons else None,
        source_wallet_evidence={"by_txid": source_evidence_store},
        receiver_wallet_evidence=receiver_evidence,
        chunks=tuple(chunk_previews),
    )


def preview_matches_existing_chunked_reconciliation(
    preview: ChunkedReconciliationPreview,
    existing: Mapping[str, Any],
) -> str | None:
    mismatches: list[str] = []
    fields = (
        ("reconciliation_status", preview.reconciliation_status, existing.get("reconciliation_status")),
        ("matched", preview.matched, bool(existing.get("matched"))),
        ("expected_chunk_count", preview.expected_chunk_count, int(existing.get("expected_chunk_count", 0))),
        ("source_chunk_count", preview.source_chunk_count, int(existing.get("source_chunk_count", 0))),
        ("sc_node_id", preview.sc_node_id, str(existing.get("sc_node_id"))),
        ("payout_address", preview.payout_address, str(existing.get("payout_address"))),
    )
    for name, preview_value, existing_value in fields:
        if preview_value != existing_value:
            mismatches.append(f"{name} mismatch (existing={existing_value!r}, preview={preview_value!r})")

    for amount_field in (
        "expected_amount_total",
        "source_amount_total",
        "receiver_amount_total",
    ):
        preview_val = getattr(preview, amount_field)
        if not _optional_decimal_equal(preview_val, existing.get(amount_field)):
            mismatches.append(f"{amount_field} mismatch")

    if preview.receiver_chunk_count is not None:
        existing_rcv = existing.get("receiver_chunk_count")
        if preview.receiver_chunk_count != (
            int(existing_rcv) if existing_rcv is not None else None
        ):
            mismatches.append("receiver_chunk_count mismatch")

    return "; ".join(mismatches) if mismatches else None


def _optional_decimal_equal(
    preview_value: Decimal | None,
    existing_value: object,
) -> bool:
    if preview_value is None and existing_value is None:
        return True
    if preview_value is None or existing_value is None:
        return False
    return _quantize_amount(preview_value) == _quantize_amount(_to_decimal(existing_value))


def chunked_reconciliation_preview_to_dict(
    preview: ChunkedReconciliationPreview,
    *,
    include_raw_evidence: bool = False,
) -> dict[str, Any]:
    source_evidence = sanitize_wallet_evidence(
        preview.source_wallet_evidence,
        include_raw=include_raw_evidence,
    )
    receiver_evidence = sanitize_wallet_evidence(
        preview.receiver_wallet_evidence,
        include_raw=include_raw_evidence,
    )
    return {
        "production_execution_id": preview.production_execution_id,
        "payout_plan_id": preview.payout_plan_id,
        "sc_node_id": preview.sc_node_id,
        "payout_address": preview.payout_address,
        "source_wallet_name": preview.source_wallet_name,
        "expected_chunk_count": preview.expected_chunk_count,
        "source_chunk_count": preview.source_chunk_count,
        "receiver_chunk_count": preview.receiver_chunk_count,
        "expected_amount_total": planner._serialize_numeric(preview.expected_amount_total),
        "source_amount_total": planner._serialize_numeric(preview.source_amount_total),
        "source_fee_total": (
            planner._serialize_numeric(preview.source_fee_total)
            if preview.source_fee_total is not None
            else None
        ),
        "receiver_amount_total": (
            planner._serialize_numeric(preview.receiver_amount_total)
            if preview.receiver_amount_total is not None
            else None
        ),
        "reconciliation_status": preview.reconciliation_status,
        "matched": preview.matched,
        "mismatch_reason": preview.mismatch_reason,
        "source_wallet_evidence": source_evidence,
        "receiver_wallet_evidence": receiver_evidence,
        "chunks": [
            {
                "production_execution_chunk_id": row.production_execution_chunk_id,
                "chunk_index": row.chunk_index,
                "txid": row.txid,
                "expected_amount": planner._serialize_numeric(row.expected_amount),
                "source_amount": (
                    planner._serialize_numeric(row.source_amount)
                    if row.source_amount is not None
                    else None
                ),
                "source_fee": (
                    planner._serialize_numeric(row.source_fee)
                    if row.source_fee is not None
                    else None
                ),
                "source_confirmations": row.source_confirmations,
                "source_blockhash": row.source_blockhash,
                "receiver_amount": (
                    planner._serialize_numeric(row.receiver_amount)
                    if row.receiver_amount is not None
                    else None
                ),
                "receiver_address": row.receiver_address,
                "receiver_confirmations": row.receiver_confirmations,
                "receiver_category": row.receiver_category,
                "row_status": row.row_status,
                "mismatch_reason": row.mismatch_reason,
            }
            for row in preview.chunks
        ],
    }


def row_to_chunked_reconciliation_dict(
    row: Mapping[str, Any],
    *,
    include_raw_evidence: bool = False,
) -> dict[str, Any]:
    source_raw = row.get("source_wallet_evidence")
    if isinstance(source_raw, str):
        try:
            source_raw = json.loads(source_raw)
        except json.JSONDecodeError:
            source_raw = None
    receiver_raw = row.get("receiver_wallet_evidence")
    if isinstance(receiver_raw, str):
        try:
            receiver_raw = json.loads(receiver_raw)
        except json.JSONDecodeError:
            receiver_raw = None
    return {
        "id": int(row["id"]),
        "production_execution_id": int(row["production_execution_id"]),
        "payout_plan_id": int(row["payout_plan_id"]),
        "sc_node_id": str(row["sc_node_id"]),
        "payout_address": row.get("payout_address"),
        "expected_chunk_count": int(row.get("expected_chunk_count", 0)),
        "source_chunk_count": int(row.get("source_chunk_count", 0)),
        "receiver_chunk_count": (
            int(row["receiver_chunk_count"])
            if row.get("receiver_chunk_count") is not None
            else None
        ),
        "expected_amount_total": planner._serialize_numeric(
            _to_decimal(row.get("expected_amount_total"))
        ),
        "source_amount_total": planner._serialize_numeric(
            _to_decimal(row.get("source_amount_total"))
        ),
        "source_fee_total": (
            planner._serialize_numeric(_to_decimal(row["source_fee_total"]))
            if row.get("source_fee_total") is not None
            else None
        ),
        "receiver_amount_total": (
            planner._serialize_numeric(_to_decimal(row["receiver_amount_total"]))
            if row.get("receiver_amount_total") is not None
            else None
        ),
        "reconciliation_status": row.get("reconciliation_status"),
        "matched": bool(row.get("matched")),
        "refusal_reason": row.get("refusal_reason"),
        "source_wallet_name": row.get("source_wallet_name"),
        "source_wallet_evidence": sanitize_wallet_evidence(
            source_raw,
            include_raw=include_raw_evidence,
        ),
        "receiver_wallet_evidence": sanitize_wallet_evidence(
            receiver_raw,
            include_raw=include_raw_evidence,
        ),
        "created_at": planner._serialize_datetime(row.get("created_at")),
        "updated_at": planner._serialize_datetime(row.get("updated_at")),
    }


def row_to_chunked_reconciliation_chunk_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "reconciliation_id": int(row["reconciliation_id"]),
        "production_execution_chunk_id": int(row["production_execution_chunk_id"]),
        "chunk_index": int(row["chunk_index"]),
        "txid": row.get("txid"),
        "expected_amount": planner._serialize_numeric(_to_decimal(row.get("expected_amount"))),
        "source_amount": (
            planner._serialize_numeric(_to_decimal(row["source_amount"]))
            if row.get("source_amount") is not None
            else None
        ),
        "source_fee": (
            planner._serialize_numeric(_to_decimal(row["source_fee"]))
            if row.get("source_fee") is not None
            else None
        ),
        "source_confirmations": (
            int(row["source_confirmations"])
            if row.get("source_confirmations") is not None
            else None
        ),
        "source_blockhash": row.get("source_blockhash"),
        "receiver_amount": (
            planner._serialize_numeric(_to_decimal(row["receiver_amount"]))
            if row.get("receiver_amount") is not None
            else None
        ),
        "receiver_address": row.get("receiver_address"),
        "receiver_confirmations": (
            int(row["receiver_confirmations"])
            if row.get("receiver_confirmations") is not None
            else None
        ),
        "receiver_category": row.get("receiver_category"),
        "row_status": row.get("row_status"),
        "refusal_reason": row.get("refusal_reason"),
        "created_at": planner._serialize_datetime(row.get("created_at")),
        "updated_at": planner._serialize_datetime(row.get("updated_at")),
    }
