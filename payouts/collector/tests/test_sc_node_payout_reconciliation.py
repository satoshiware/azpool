from __future__ import annotations

import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

from payouts.collector.app import admin_readonly
from payouts.collector.app import sc_node_payout_reconciliation as reconciliation
from payouts.scripts import sc_node_payout_reconciliation as reconciliation_cli


_WALLET_SEND_KEYWORDS = re.compile(
    r"\b("
    r"sendmany|sendtoaddress|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|privkey"
    r")\b",
    re.IGNORECASE,
)

_TXID = "838d4ac398cd3a570f0601389b55334099c14f6484571397f2be35d6df758b00"
_ADDRESS = "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv"
_AMOUNT = Decimal("121.875000000000")


def _confirmed_execution(*, status: str = "confirmed") -> dict[str, object]:
    return {
        "id": 1,
        "payout_plan_id": 1,
        "source_wallet_name": "wallet",
        "status": status,
        "txid": _TXID,
    }


def _execution_row() -> dict[str, object]:
    return {
        "id": 10,
        "sc_node_id": "sc-2",
        "payout_address": _ADDRESS,
        "payout_amount": _AMOUNT,
    }


def _source_payload(*, confirmations: int = 3) -> dict[str, object]:
    return {
        "txid": _TXID,
        "confirmations": confirmations,
        "fee": -0.0001,
        "amount": -_AMOUNT,
        "details": [
            {
                "address": _ADDRESS,
                "category": "send",
                "amount": -_AMOUNT,
            }
        ],
    }


def _receiver_row(
    *,
    amount: str | Decimal = _AMOUNT,
    address: str = _ADDRESS,
    category: str = "receive",
    confirmations: int = 3,
) -> dict[str, object]:
    return {
        "txid": _TXID,
        "confirmations": confirmations,
        "amount": str(amount),
        "category": category,
        "address": address,
    }


def test_parse_source_gettransaction_extracts_fields() -> None:
    evidence = reconciliation.parse_source_gettransaction(_source_payload(), _TXID)
    assert evidence.txid == _TXID
    assert evidence.confirmations == 3
    assert evidence.fee == Decimal("0.000100000000")
    assert evidence.amount == _AMOUNT


def test_parse_receiver_transactions_json_finds_matching_txid() -> None:
    evidence = reconciliation.parse_receiver_transactions_json(
        [_receiver_row(), {"txid": "other", "address": "x", "amount": "1", "category": "receive"}],
        _TXID,
    )
    assert evidence is not None
    assert evidence.txid == _TXID
    assert evidence.address == _ADDRESS
    assert evidence.amount == _AMOUNT
    assert evidence.category == "receive"
    assert evidence.confirmations == 3


def test_compare_reconciliation_matched_when_all_evidence_aligns() -> None:
    source = reconciliation.parse_source_gettransaction(_source_payload(), _TXID)
    receiver = reconciliation.parse_receiver_transactions_json([_receiver_row()], _TXID)
    preview = reconciliation.compare_reconciliation(
        _confirmed_execution(),
        [_execution_row()],
        source,
        receiver,
    )
    assert preview.reconciliation_status == reconciliation.RECONCILIATION_STATUS_MATCHED
    assert preview.matched is True
    assert preview.mismatch_reason is None
    assert preview.rows[0].row_status == reconciliation.ROW_STATUS_MATCHED


def test_compare_reconciliation_mismatch_when_receiver_amount_differs() -> None:
    source = reconciliation.parse_source_gettransaction(_source_payload(), _TXID)
    receiver = reconciliation.parse_receiver_transactions_json(
        [_receiver_row(amount="100")],
        _TXID,
    )
    assert receiver is not None
    preview = reconciliation.compare_reconciliation(
        _confirmed_execution(),
        [_execution_row()],
        source,
        receiver,
    )
    assert preview.reconciliation_status == reconciliation.RECONCILIATION_STATUS_MISMATCH
    assert preview.matched is False
    assert preview.rows[0].row_status == reconciliation.ROW_STATUS_MISMATCH
    assert preview.rows[0].mismatch_reason is not None
    assert "amount mismatch" in preview.rows[0].mismatch_reason


def test_compare_reconciliation_mismatch_when_receiver_address_differs() -> None:
    source = reconciliation.parse_source_gettransaction(_source_payload(), _TXID)
    receiver = reconciliation.parse_receiver_transactions_json(
        [_receiver_row(address="az1other")],
        _TXID,
    )
    assert receiver is not None
    preview = reconciliation.compare_reconciliation(
        _confirmed_execution(),
        [_execution_row()],
        source,
        receiver,
    )
    assert preview.matched is False
    assert preview.rows[0].mismatch_reason is not None
    assert "address mismatch" in preview.rows[0].mismatch_reason


def test_compare_reconciliation_mismatch_when_source_confirmations_pending() -> None:
    source = reconciliation.parse_source_gettransaction(
        _source_payload(confirmations=0),
        _TXID,
    )
    receiver = reconciliation.parse_receiver_transactions_json([_receiver_row()], _TXID)
    preview = reconciliation.compare_reconciliation(
        _confirmed_execution(),
        [_execution_row()],
        source,
        receiver,
    )
    assert preview.matched is False
    assert preview.reconciliation_status == reconciliation.RECONCILIATION_STATUS_MISMATCH
    assert preview.mismatch_reason is not None
    assert "source confirmations pending" in preview.mismatch_reason


def _source_evidence_with_hex(*, hex_body: str = "ab" * 200) -> dict[str, object]:
    return {
        "txid": _TXID,
        "confirmations": 3,
        "amount": -121.875,
        "fee": -0.0001,
        "hex": hex_body,
        "details": [{"address": _ADDRESS, "category": "send", "amount": -121.875}],
        "blockhash": "0000000000000000000000000000000000000000000000000000000000000001",
        "blockheight": 100,
        "time": 1710000000,
        "timereceived": 1710000001,
        "walletconflicts": [],
        "bip125-replaceable": "no",
    }


def test_sanitize_source_wallet_evidence_omits_hex_by_default() -> None:
    raw = _source_evidence_with_hex()
    sanitized = reconciliation.sanitize_source_wallet_evidence(raw)
    assert sanitized is not None
    assert "hex" not in sanitized
    assert sanitized["hex_omitted"] is True
    assert sanitized["hex_length"] == len(raw["hex"])
    assert sanitized["txid"] == _TXID
    assert sanitized["confirmations"] == 3
    assert sanitized["details"] == raw["details"]
    assert sanitized["blockhash"] == raw["blockhash"]
    assert sanitized["bip125-replaceable"] == "no"


def test_sanitize_source_wallet_evidence_include_raw_preserves_hex() -> None:
    raw = _source_evidence_with_hex()
    result = reconciliation.sanitize_source_wallet_evidence(raw, include_raw=True)
    assert result is not None
    assert result["hex"] == raw["hex"]
    assert "hex_omitted" not in result


def test_row_to_reconciliation_dict_defaults_to_sanitized_source_evidence() -> None:
    raw = _source_evidence_with_hex(hex_body="cd" * 50)
    row = {
        "id": 1,
        "production_execution_id": 1,
        "payout_plan_id": 1,
        "source_wallet_name": "wallet",
        "txid": _TXID,
        "reconciliation_status": "matched",
        "expected_amount": _AMOUNT,
        "expected_address": _ADDRESS,
        "matched": True,
        "source_wallet_evidence": raw,
        "receiver_wallet_evidence": _receiver_row(),
    }
    result = reconciliation.row_to_reconciliation_dict(row)
    evidence = result["source_wallet_evidence"]
    assert isinstance(evidence, dict)
    assert "hex" not in evidence
    assert evidence["hex_omitted"] is True
    assert evidence["hex_length"] == 100


def test_row_to_reconciliation_dict_include_raw_evidence_preserves_hex() -> None:
    raw = _source_evidence_with_hex(hex_body="ef" * 10)
    row = {
        "id": 1,
        "production_execution_id": 1,
        "payout_plan_id": 1,
        "source_wallet_name": "wallet",
        "txid": _TXID,
        "reconciliation_status": "matched",
        "expected_amount": _AMOUNT,
        "expected_address": _ADDRESS,
        "matched": True,
        "source_wallet_evidence": raw,
        "receiver_wallet_evidence": None,
    }
    result = reconciliation.row_to_reconciliation_dict(row, include_raw_evidence=True)
    evidence = result["source_wallet_evidence"]
    assert isinstance(evidence, dict)
    assert evidence["hex"] == raw["hex"]


def _matched_preview() -> reconciliation.ReconciliationPreview:
    source = reconciliation.parse_source_gettransaction(_source_payload(), _TXID)
    receiver = reconciliation.parse_receiver_transactions_json([_receiver_row()], _TXID)
    return reconciliation.compare_reconciliation(
        _confirmed_execution(),
        [_execution_row()],
        source,
        receiver,
    )


def _existing_reconciliation_row(
    preview: reconciliation.ReconciliationPreview,
    *,
    reconciliation_id: int = 1,
) -> dict[str, object]:
    return {
        "id": reconciliation_id,
        "production_execution_id": preview.production_execution_id,
        "payout_plan_id": preview.payout_plan_id,
        "source_wallet_name": preview.source_wallet_name,
        "txid": preview.txid,
        "reconciliation_status": preview.reconciliation_status,
        "expected_amount": preview.expected_amount,
        "expected_address": preview.expected_address,
        "matched": preview.matched,
        "mismatch_reason": preview.mismatch_reason,
        "receiver_amount": preview.receiver_amount,
        "receiver_address": preview.receiver_address,
        "receiver_category": preview.receiver_category,
    }


def test_build_reconciliation_by_execution_txid_sql_is_select_only() -> None:
    sql = reconciliation.build_reconciliation_by_execution_txid_sql()
    admin_readonly.assert_readonly_sql(sql)
    assert "production_execution_id = %(production_execution_id)s" in sql
    assert "receiver_amount" in sql


def test_preview_matches_existing_reconciliation_returns_none_when_aligned() -> None:
    preview = _matched_preview()
    existing = _existing_reconciliation_row(preview)
    assert reconciliation.preview_matches_existing_reconciliation(preview, existing) is None


def test_preview_matches_existing_reconciliation_refuses_on_status_mismatch() -> None:
    preview = _matched_preview()
    existing = _existing_reconciliation_row(preview)
    existing["reconciliation_status"] = "mismatch"
    refusal = reconciliation.preview_matches_existing_reconciliation(preview, existing)
    assert refusal is not None
    assert "reconciliation_status mismatch" in refusal


def test_preview_matches_existing_reconciliation_refuses_on_receiver_amount() -> None:
    preview = _matched_preview()
    existing = _existing_reconciliation_row(preview)
    existing["receiver_amount"] = Decimal("1")
    refusal = reconciliation.preview_matches_existing_reconciliation(preview, existing)
    assert refusal is not None
    assert "receiver_amount mismatch" in refusal


def test_record_command_idempotent_replay_shape() -> None:
    record_block = Path(reconciliation_cli.__file__).read_text(encoding="utf-8")
    assert "idempotent_replay" in record_block
    assert "preview_matches_existing_reconciliation" in record_block
    assert '"reconciliation_id": reconciliation_id' in record_block


def test_record_insert_params_wrap_jsonb_evidence() -> None:
    source = reconciliation.parse_source_gettransaction(_source_payload(), _TXID)
    receiver = reconciliation.parse_receiver_transactions_json([_receiver_row()], _TXID)
    preview = reconciliation.compare_reconciliation(
        _confirmed_execution(),
        [_execution_row()],
        source,
        receiver,
    )
    params = reconciliation_cli._reconciliation_insert_params(preview, notes="audit")
    assert isinstance(params["source_wallet_evidence"], Jsonb)
    assert isinstance(params["receiver_wallet_evidence"], Jsonb)
    assert params["source_wallet_evidence"].obj == preview.source_wallet_evidence
    assert params["receiver_wallet_evidence"].obj == preview.receiver_wallet_evidence


def test_record_insert_params_receiver_jsonb_none_when_missing() -> None:
    source = reconciliation.parse_source_gettransaction(_source_payload(), _TXID)
    preview = reconciliation.compare_reconciliation(
        _confirmed_execution(),
        [_execution_row()],
        source,
        None,
    )
    params = reconciliation_cli._reconciliation_insert_params(preview, notes=None)
    assert isinstance(params["source_wallet_evidence"], Jsonb)
    assert params["receiver_wallet_evidence"] is None


def test_compare_reconciliation_draft_when_receiver_missing() -> None:
    source = reconciliation.parse_source_gettransaction(_source_payload(), _TXID)
    preview = reconciliation.compare_reconciliation(
        _confirmed_execution(),
        [_execution_row()],
        source,
        None,
    )
    assert preview.reconciliation_status == reconciliation.RECONCILIATION_STATUS_DRAFT
    assert preview.matched is False
    assert preview.mismatch_reason == "receiver evidence missing"
    assert preview.rows[0].row_status == reconciliation.ROW_STATUS_DRAFT


def test_preview_command_does_not_reference_insert_sql() -> None:
    source = reconciliation_cli.__file__
    text = Path(source).read_text(encoding="utf-8")
    preview_block = text.split("def _cmd_preview")[1].split("def _cmd_record")[0]
    assert "build_insert_reconciliation" not in preview_block


def test_insert_sql_targets_only_reconciliation_tables() -> None:
    header_sql = reconciliation.build_insert_reconciliation_sql()
    row_sql = reconciliation.build_insert_reconciliation_row_sql()
    for sql in (header_sql, row_sql):
        lowered = sql.lower()
        assert "insert into" in lowered
        tables = re.findall(r"insert\s+into\s+([a-z0-9_]+)", lowered)
        assert tables
        for table in tables:
            assert table in {
                "sc_node_payout_reconciliations",
                "sc_node_payout_reconciliation_rows",
            }


def test_admin_reconciliation_sql_is_select_only() -> None:
    for sql in (
        admin_readonly.build_payout_reconciliations_sql(),
        admin_readonly.build_payout_reconciliation_details_sql(1),
        admin_readonly.build_payout_reconciliation_rows_sql(1),
    ):
        admin_readonly.assert_readonly_sql(sql)
        assert "sc_node_payout_reconciliation" in sql


def test_gettransaction_argv_is_explicit_list_without_shell_true() -> None:
    argv = reconciliation_cli._gettransaction_argv(
        azc_bin="/usr/local/bin/azc-payout-readonly",
        source_wallet_name="wallet",
        txid=_TXID,
    )
    assert argv == [
        "/usr/local/bin/azc-payout-readonly",
        "-rpcwallet=wallet",
        "gettransaction",
        _TXID,
    ]
    script_text = Path(reconciliation_cli.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in script_text
    assert "subprocess.run" in script_text


def test_assert_no_wallet_send_keywords_blocks_sendtoaddress() -> None:
    with pytest.raises(ValueError, match="wallet send"):
        reconciliation.assert_no_wallet_send_keywords("sendtoaddress az1abc 1")


def test_confirmed_production_execution_sql_filters_status() -> None:
    sql = reconciliation.build_confirmed_production_execution_sql(1)
    reconciliation.assert_no_wallet_send_keywords(sql)
    assert "status = 'confirmed'" in sql
    assert "INSERT" not in sql.upper()


def test_implementation_files_have_no_wallet_send_keywords() -> None:
    guard_block = re.compile(
        r"_WALLET_SEND_KEYWORDS = re\.compile\([\s\S]*?\)\n",
        re.MULTILINE,
    )
    for rel in (
        "payouts/collector/app/sc_node_payout_reconciliation.py",
        "payouts/scripts/sc_node_payout_reconciliation.py",
    ):
        text = (AZPOOL_ROOT / rel).read_text(encoding="utf-8")
        scrubbed = guard_block.sub("", text, count=1)
        assert _WALLET_SEND_KEYWORDS.search(scrubbed) is None


def test_script_has_no_send_rpc_calls() -> None:
    source = (
        AZPOOL_ROOT / "payouts/scripts/sc_node_payout_reconciliation.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "sendtoaddress",
        "sendmany",
        "sendrawtransaction",
        "signrawtransaction",
        "createrawtransaction",
        "walletpassphrase",
    ):
        assert forbidden not in source
