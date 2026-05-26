from __future__ import annotations

import json
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

from payouts.collector.app import admin_readonly
from payouts.collector.app import sc_node_chunked_payout_reconciliation as chunked_recon
from payouts.scripts import sc_node_chunked_payout_reconciliation as chunked_cli

_ADDRESS = "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv"
_PLANNED = Decimal("223.125000000000")
_EXECUTION_ID = 3
_PLAN_ID = 2

_CHUNK_TXIDS = [
    ("d19132b399ebd813e7e03c7bf8c8515aa5bac5e3c15961c41057255b6645d8c9", Decimal("25")),
    ("242b27d12e0d2a0c031aa9be7e68ef3b6878bf422334b8c9d9bad40b8c0a99c8", Decimal("25")),
    ("6b0e320181daea9d673d84e27cbbb168ad8c01263d2a1dcbbeffb11595d77225", Decimal("25")),
    ("fe5eb173ec3e20c2b56a64c0b297ef3fc677d4dde651d6190f43c6261e833238", Decimal("25")),
    ("b712ccd93534eed4065410e6132ffdad4246f85af077ae8ce779387303411df3", Decimal("25")),
    ("d0a753022cde0c0c2fef5fc82c313da54db6adfa8a18da0ab016669e915670d1", Decimal("25")),
    ("a0547b0694afaa498d611afbbdc0b7e57eda5a99d094f929ce402c0dc2cfe579", Decimal("25")),
    ("488c8a229086f848a8347247d0e2533da42961dd0ff4e706e6ab7296bb3039a7", Decimal("25")),
    ("cb59d91b888d115222fd6045d7106b1b6ada531bfbeef77ad5ab970df5bd7f32", Decimal("23.125")),
]

_FORBIDDEN_RPC = re.compile(
    r"\b("
    r"sendmany|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|privkey"
    r")\b",
    re.IGNORECASE,
)


def _execution() -> dict[str, object]:
    return {
        "id": _EXECUTION_ID,
        "payout_plan_id": _PLAN_ID,
        "status": "confirmed",
        "planned_amount_total": _PLANNED,
        "source_wallet_name": "wallet",
    }


def _chunk_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, (txid, amount) in enumerate(_CHUNK_TXIDS, start=1):
        rows.append(
            {
                "id": 100 + index,
                "production_execution_id": _EXECUTION_ID,
                "sc_node_id": "sc-2",
                "payout_address": _ADDRESS,
                "chunk_index": index,
                "chunk_amount": amount,
                "chunk_status": "confirmed",
                "txid": txid,
            }
        )
    return rows


def _source_payload(txid: str, amount: Decimal, *, confirmations: int = 3) -> dict[str, object]:
    return {
        "txid": txid,
        "confirmations": confirmations,
        "fee": -0.0001,
        "amount": -amount,
        "blockhash": "0000000000000000000000000000000000000000000000000000000000000abc",
        "details": [{"address": _ADDRESS, "category": "send", "amount": -amount}],
    }


def _receiver_rows() -> list[dict[str, object]]:
    return [
        {
            "txid": txid,
            "confirmations": 3,
            "amount": str(amount),
            "category": "receive",
            "address": _ADDRESS,
        }
        for txid, amount in _CHUNK_TXIDS
    ]


def _build_preview(
    *,
    receiver_rows: list[dict[str, object]] | None,
) -> chunked_recon.ChunkedReconciliationPreview:
    source_by_txid = {
        txid: chunked_recon.parse_source_gettransaction(_source_payload(txid, amount), txid)
        for txid, amount in _CHUNK_TXIDS
    }
    return chunked_recon.build_chunked_reconciliation_preview(
        execution=_execution(),
        chunks=_chunk_rows(),
        source_wallet_name="wallet",
        source_by_txid=source_by_txid,
        receiver_rows=receiver_rows,
    )


def test_parse_source_negative_amount_becomes_positive() -> None:
    txid, amount = _CHUNK_TXIDS[0]
    evidence = chunked_recon.parse_source_gettransaction(_source_payload(txid, amount), txid)
    assert evidence.amount == Decimal("25.000000000000")


def test_preview_source_only_without_receiver_json() -> None:
    preview = _build_preview(receiver_rows=None)
    assert preview.reconciliation_status == chunked_recon.RECONCILIATION_STATUS_SOURCE_ONLY
    assert preview.matched is False
    assert preview.expected_chunk_count == 9
    assert preview.source_chunk_count == 9
    assert preview.receiver_chunk_count is None
    assert preview.expected_amount_total == _PLANNED


def test_preview_matched_with_receiver_json() -> None:
    preview = _build_preview(receiver_rows=_receiver_rows())
    assert preview.reconciliation_status == chunked_recon.RECONCILIATION_STATUS_MATCHED
    assert preview.matched is True
    assert preview.receiver_chunk_count == 9
    assert preview.receiver_amount_total == _PLANNED


def test_preview_mismatch_missing_receiver_txid() -> None:
    receiver = _receiver_rows()[:-1]
    preview = _build_preview(receiver_rows=receiver)
    assert preview.matched is False
    assert preview.reconciliation_status == chunked_recon.RECONCILIATION_STATUS_MISMATCH
    assert "receiver" in (preview.mismatch_reason or "")


def test_preview_mismatch_receiver_amount() -> None:
    receiver = _receiver_rows()
    receiver[0] = dict(receiver[0])
    receiver[0]["amount"] = "1"
    preview = _build_preview(receiver_rows=receiver)
    assert preview.reconciliation_status == chunked_recon.RECONCILIATION_STATUS_MISMATCH
    assert any(
        row.mismatch_reason and "amount mismatch" in row.mismatch_reason
        for row in preview.chunks
    )


def test_preview_mismatch_receiver_address() -> None:
    receiver = _receiver_rows()
    receiver[0] = dict(receiver[0])
    receiver[0]["address"] = "az1other"
    preview = _build_preview(receiver_rows=receiver)
    assert preview.reconciliation_status == chunked_recon.RECONCILIATION_STATUS_MISMATCH
    assert any(
        row.mismatch_reason and "address mismatch" in row.mismatch_reason
        for row in preview.chunks
    )


def test_preview_mismatch_source_confirmations_pending() -> None:
    txid, amount = _CHUNK_TXIDS[0]
    source_by_txid = {
        txid: chunked_recon.parse_source_gettransaction(
            _source_payload(txid, amount, confirmations=0),
            txid,
        )
    }
    for other_txid, other_amount in _CHUNK_TXIDS[1:]:
        source_by_txid[other_txid] = chunked_recon.parse_source_gettransaction(
            _source_payload(other_txid, other_amount),
            other_txid,
        )
    preview = chunked_recon.build_chunked_reconciliation_preview(
        execution=_execution(),
        chunks=_chunk_rows(),
        source_wallet_name="wallet",
        source_by_txid=source_by_txid,
        receiver_rows=None,
    )
    assert preview.reconciliation_status == chunked_recon.RECONCILIATION_STATUS_MISMATCH


def test_insert_sql_targets_only_chunked_reconciliation_tables() -> None:
    for sql in (
        chunked_recon.build_insert_chunked_reconciliation_sql(),
        chunked_recon.build_insert_chunked_reconciliation_chunk_sql(),
    ):
        lowered = sql.lower()
        for table in re.findall(r"insert\s+into\s+([a-z0-9_]+)", lowered):
            assert table in chunked_recon._CHUNKED_RECON_INSERT_TABLES


def test_preview_matches_existing_returns_none_when_aligned() -> None:
    preview = _build_preview(receiver_rows=_receiver_rows())
    existing = {
        "reconciliation_status": preview.reconciliation_status,
        "matched": preview.matched,
        "expected_chunk_count": preview.expected_chunk_count,
        "source_chunk_count": preview.source_chunk_count,
        "receiver_chunk_count": preview.receiver_chunk_count,
        "sc_node_id": preview.sc_node_id,
        "payout_address": preview.payout_address,
        "expected_amount_total": preview.expected_amount_total,
        "source_amount_total": preview.source_amount_total,
        "receiver_amount_total": preview.receiver_amount_total,
    }
    assert chunked_recon.preview_matches_existing_chunked_reconciliation(preview, existing) is None


def test_preview_matches_existing_refuses_on_mismatch() -> None:
    preview = _build_preview(receiver_rows=_receiver_rows())
    existing = {
        "reconciliation_status": preview.reconciliation_status,
        "matched": preview.matched,
        "expected_chunk_count": preview.expected_chunk_count,
        "source_chunk_count": preview.source_chunk_count,
        "receiver_chunk_count": 1,
        "sc_node_id": preview.sc_node_id,
        "payout_address": preview.payout_address,
        "expected_amount_total": preview.expected_amount_total,
        "source_amount_total": preview.source_amount_total,
        "receiver_amount_total": preview.receiver_amount_total,
    }
    refusal = chunked_recon.preview_matches_existing_chunked_reconciliation(preview, existing)
    assert refusal is not None
    assert "receiver_chunk_count" in refusal


def test_details_sanitizes_hex_by_default() -> None:
    raw = {"by_txid": {_CHUNK_TXIDS[0][0]: {"hex": "ab" * 100, "txid": _CHUNK_TXIDS[0][0]}}}
    row = {
        "id": 1,
        "production_execution_id": _EXECUTION_ID,
        "payout_plan_id": _PLAN_ID,
        "sc_node_id": "sc-2",
        "payout_address": _ADDRESS,
        "expected_chunk_count": 9,
        "source_chunk_count": 9,
        "receiver_chunk_count": 9,
        "expected_amount_total": _PLANNED,
        "source_amount_total": _PLANNED,
        "source_fee_total": None,
        "receiver_amount_total": _PLANNED,
        "reconciliation_status": "matched",
        "matched": True,
        "refusal_reason": None,
        "source_wallet_name": "wallet",
        "source_wallet_evidence": raw,
        "receiver_wallet_evidence": None,
    }
    result = chunked_recon.row_to_chunked_reconciliation_dict(row, include_raw_evidence=False)
    evidence = result["source_wallet_evidence"]
    assert isinstance(evidence, dict)
    by_txid = evidence["by_txid"]
    first = by_txid[_CHUNK_TXIDS[0][0]]
    assert "hex" not in first
    assert first["hex_omitted"] is True


def test_include_raw_evidence_preserves_hex() -> None:
    raw = {"by_txid": {_CHUNK_TXIDS[0][0]: {"hex": "cd" * 10, "txid": _CHUNK_TXIDS[0][0]}}}
    row = {
        "id": 1,
        "production_execution_id": _EXECUTION_ID,
        "payout_plan_id": _PLAN_ID,
        "sc_node_id": "sc-2",
        "payout_address": _ADDRESS,
        "expected_chunk_count": 9,
        "source_chunk_count": 9,
        "receiver_chunk_count": None,
        "expected_amount_total": _PLANNED,
        "source_amount_total": _PLANNED,
        "source_fee_total": None,
        "receiver_amount_total": None,
        "reconciliation_status": "source_only",
        "matched": False,
        "refusal_reason": None,
        "source_wallet_name": "wallet",
        "source_wallet_evidence": raw,
        "receiver_wallet_evidence": None,
    }
    result = chunked_recon.row_to_chunked_reconciliation_dict(row, include_raw_evidence=True)
    by_txid = result["source_wallet_evidence"]["by_txid"]
    assert by_txid[_CHUNK_TXIDS[0][0]]["hex"] == "cd" * 10


def test_gettransaction_argv_explicit_list_no_shell_true() -> None:
    txid = _CHUNK_TXIDS[0][0]
    argv = chunked_cli._gettransaction_argv(
        azc_bin="/usr/local/bin/azc-payout-readonly",
        source_wallet_name="wallet",
        txid=txid,
    )
    assert argv == [
        "/usr/local/bin/azc-payout-readonly",
        "-rpcwallet=wallet",
        "gettransaction",
        txid,
    ]
    script = Path(chunked_cli.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in script
    assert "subprocess.run" in script


def test_admin_chunked_reconciliation_sql_is_select_only() -> None:
    sql = chunked_recon.build_chunked_reconciliations_sql()
    admin_readonly.assert_readonly_sql(sql)
    assert "sc_node_chunked_payout_reconciliations" in sql


def test_record_command_idempotent_replay_shape() -> None:
    record_block = Path(chunked_cli.__file__).read_text(encoding="utf-8")
    assert "idempotent_replay" in record_block
    assert "preview_matches_existing_chunked_reconciliation" in record_block
    assert "for chunk_row in preview.chunks" in record_block


def test_record_inserts_one_row_per_preview_chunk() -> None:
    preview = _build_preview(receiver_rows=_receiver_rows())
    assert len(preview.chunks) == 9
    assert preview.expected_chunk_count == 9
    assert sum(row.expected_amount for row in preview.chunks) == _PLANNED


def test_jsonb_evidence_wrapper() -> None:
    from psycopg.types.json import Jsonb

    preview = _build_preview(receiver_rows=_receiver_rows())
    source = chunked_cli._jsonb_evidence(preview.source_wallet_evidence)
    receiver = chunked_cli._jsonb_evidence(preview.receiver_wallet_evidence)
    assert isinstance(source, Jsonb)
    assert isinstance(receiver, Jsonb)
    assert source.obj == preview.source_wallet_evidence
    assert receiver.obj == preview.receiver_wallet_evidence
    assert chunked_cli._jsonb_evidence(None) is None


def test_script_has_no_forbidden_rpc() -> None:
    script = (AZPOOL_ROOT / "payouts/scripts/sc_node_chunked_payout_reconciliation.py").read_text(
        encoding="utf-8"
    )
    assert "sendtoaddress" not in script
    assert _FORBIDDEN_RPC.search(script) is None
