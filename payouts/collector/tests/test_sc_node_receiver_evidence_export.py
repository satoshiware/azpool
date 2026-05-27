from __future__ import annotations

import sys
from pathlib import Path

import pytest

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

from payouts.collector.app import sc_node_receiver_evidence_export as export
from payouts.scripts import sc_node_receiver_evidence_export as export_cli


_ALLOWLIST = frozenset({"SC2TESTWALLETLISTENER"})


def test_assert_allowed_rpc_method_accepts_listtransactions() -> None:
    export.assert_allowed_rpc_method("listtransactions")


def test_assert_allowed_rpc_method_accepts_gettransaction() -> None:
    export.assert_allowed_rpc_method("gettransaction")


@pytest.mark.parametrize(
    "method",
    [
        "sendtoaddress",
        "sendmany",
        "walletpassphrase",
        "getbalances",
        "dumpprivkey",
        "importprivkey",
    ],
)
def test_assert_allowed_rpc_method_rejects_dangerous(method: str) -> None:
    with pytest.raises(ValueError, match="forbidden|not allowlisted"):
        export.assert_allowed_rpc_method(method)


def test_denied_production_wallets_rejected() -> None:
    with pytest.raises(ValueError, match="denied"):
        export.assert_receiver_evidence_wallet_allowed(
            "wallet",
            allowlist=_ALLOWLIST,
        )
    with pytest.raises(ValueError, match="denied"):
        export.assert_receiver_evidence_wallet_allowed(
            "SUPPORT",
            allowlist=_ALLOWLIST,
        )


def test_wallet_must_be_in_allowlist() -> None:
    with pytest.raises(ValueError, match="PAYOUT_RECEIVER_EVIDENCE_WALLET_ALLOWLIST"):
        export.assert_receiver_evidence_wallet_allowed(
            "SC2TESTWALLETLISTENER",
            allowlist=frozenset(),
        )
    with pytest.raises(ValueError, match="not in receiver evidence allowlist"):
        export.assert_receiver_evidence_wallet_allowed(
            "UNKNOWNWALLET",
            allowlist=_ALLOWLIST,
        )


def test_listtransactions_argv_explicit_list_no_shell_true() -> None:
    argv = export.build_listtransactions_argv(
        azc_bin="/usr/local/bin/azc-payout-readonly",
        wallet="SC2TESTWALLETLISTENER",
        count=100,
        allowlist=_ALLOWLIST,
    )
    assert argv == [
        "/usr/local/bin/azc-payout-readonly",
        "-rpcwallet=SC2TESTWALLETLISTENER",
        "listtransactions",
        "*",
        "100",
        "0",
    ]
    script = Path(export_cli.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in script
    assert "subprocess.run" in script


def test_gettransaction_argv_uses_allowlisted_rpc_only() -> None:
    txid = "abc" * 21
    argv = export.build_gettransaction_argv(
        azc_bin="/usr/local/bin/azc-payout-readonly",
        wallet="SC2TESTWALLETLISTENER",
        txid=txid,
        allowlist=_ALLOWLIST,
    )
    assert argv[-2:] == ["gettransaction", txid]


def test_sanitize_transaction_row_removes_hex() -> None:
    row = {"txid": "abc", "category": "receive", "hex": "deadbeef", "amount": 1.0}
    sanitized = export.sanitize_transaction_row(row)
    assert "hex" not in sanitized
    assert sanitized["txid"] == "abc"


def test_filter_receive_transactions() -> None:
    rows = [
        {"category": "receive", "txid": "a"},
        {"category": "send", "txid": "b"},
        {"category": "Receive", "txid": "c"},
    ]
    filtered = export.filter_receive_transactions(rows)
    assert [row["txid"] for row in filtered] == ["a", "c"]


def test_build_receiver_evidence_export_receive_only() -> None:
    payload = export.build_receiver_evidence_export(
        wallet="SC2TESTWALLETLISTENER",
        transactions=[
            {"category": "receive", "txid": "a", "amount": 1},
            {"category": "send", "txid": "b", "amount": 2},
        ],
        count=50,
        receive_only=True,
        txid_details=None,
    )
    assert payload["transaction_count"] == 1
    assert payload["transactions"][0]["txid"] == "a"
    export.validate_export_json(payload)


def test_dangerous_azc_bin_rejected() -> None:
    with pytest.raises(ValueError, match="wallet send"):
        export.build_listtransactions_argv(
            azc_bin="/tmp/azc-sendtoaddress",
            wallet="SC2TESTWALLETLISTENER",
            count=10,
            allowlist=_ALLOWLIST,
        )
