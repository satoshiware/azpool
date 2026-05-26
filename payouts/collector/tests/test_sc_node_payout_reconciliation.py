from __future__ import annotations

import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

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
