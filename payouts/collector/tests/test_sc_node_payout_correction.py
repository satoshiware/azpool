from __future__ import annotations

import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

from payouts.collector.app import sc_node_payout_correction as correction
from payouts.collector.app import sc_node_payout_planner as planner


_MUTATING_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE|VACUUM|CALL)\b",
    re.IGNORECASE,
)

_WALLET_SEND_KEYWORDS = re.compile(
    r"\b("
    r"sendmany|sendtoaddress|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|privkey"
    r")\b",
    re.IGNORECASE,
)


def _correction(
    *,
    correction_id: int = 1,
    sc_node_id: str = "sc-2",
    amount: str = "1.875",
    status: str = correction.CORRECTION_STATUS_DRAFT,
    wallet_name: str = "wallet",
    credit_run_id: int | None = 5,
) -> dict[str, object]:
    return {
        "id": correction_id,
        "sc_node_id": sc_node_id,
        "wallet_name": wallet_name,
        "amount": Decimal(amount),
        "direction": correction.CORRECTION_DIRECTION_OFFSET_DEBIT,
        "reason_code": "boundary_overpayment",
        "status": status,
        "related_credit_run_id": credit_run_id,
    }


def test_apply_correction_to_row_amounts_reduces_net_payout() -> None:
    gross, applied, net = correction.apply_correction_to_row_amounts(
        gross_credit_amount=Decimal("61.875"),
        correction_amount=Decimal("1.875"),
    )
    assert gross == Decimal("61.875000000000")
    assert applied == Decimal("1.875000000000")
    assert net == Decimal("60.000000000000")


def test_correction_cannot_exceed_gross_credit_amount() -> None:
    refusal = correction.evaluate_correction_amount_refusal(
        gross_credit_amount=Decimal("1.875"),
        correction_amount=Decimal("2"),
    )
    assert refusal is not None
    assert "exceeds gross credit amount" in refusal


def test_correction_for_plan_refuses_wallet_mismatch() -> None:
    refusal = correction.evaluate_correction_for_plan_refusal(
        correction=_correction(wallet_name="other-wallet"),
        wallet_name="wallet",
        credit_run_id=5,
        sc_node_ids={"sc-2"},
    )
    assert refusal is not None
    assert "wallet_name" in refusal


def test_correction_for_plan_refuses_sc_node_mismatch() -> None:
    refusal = correction.evaluate_correction_for_plan_refusal(
        correction=_correction(sc_node_id="sc-3"),
        wallet_name="wallet",
        credit_run_id=5,
        sc_node_ids={"sc-2"},
    )
    assert refusal is not None
    assert "sc_node_id" in refusal


def test_correction_for_plan_refuses_already_applied() -> None:
    refusal = correction.evaluate_correction_for_plan_refusal(
        correction=_correction(status=correction.CORRECTION_STATUS_APPLIED),
        wallet_name="wallet",
        credit_run_id=5,
        sc_node_ids={"sc-2"},
    )
    assert refusal == "payout correction already applied"


def test_planner_preview_applies_correction_to_net_amount() -> None:
    preview = planner.build_payout_plan_preview(
        credit_run_id=5,
        wallet_name="wallet",
        reserve_fraction=Decimal("0.50"),
        trusted_balance_snapshot=Decimal("500"),
        credit_run={
            "id": 5,
            "wallet_name": "wallet",
            "maturity_status": "mature",
            "status": "draft",
        },
        credits=[
            {
                "id": 10,
                "credit_run_id": 5,
                "sc_node_id": "sc-2",
                "sc_node_display_name": "SC 2",
                "credit_amount": Decimal("61.875"),
                "credit_status": "draft",
            }
        ],
        address_lookup={
            "sc-2": [
                {
                    "sc_node_id": "sc-2",
                    "payout_address": "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv",
                }
            ]
        },
        correction=_correction(),
    )
    assert preview.plan_allowed is True
    assert preview.gross_planned_amount_total == Decimal("61.875")
    assert preview.correction_amount_total == Decimal("1.875")
    assert preview.planned_amount_total == Decimal("60.000")
    row = preview.rows[0]
    assert row.gross_credit_amount == Decimal("61.875")
    assert row.correction_amount == Decimal("1.875")
    assert row.payout_amount == Decimal("60.000")
    payload = planner.payout_plan_preview_to_dict(preview)
    assert payload["rows"][0]["net_payout_amount"] == "60.000000000000"
    assert Decimal(payload["rows"][0]["gross_credit_amount"]) == Decimal("61.875")


def test_planner_without_correction_keeps_existing_behavior() -> None:
    preview = planner.build_payout_plan_preview(
        credit_run_id=1,
        wallet_name="wallet",
        reserve_fraction=Decimal("0.50"),
        trusted_balance_snapshot=Decimal("300"),
        credit_run={
            "id": 1,
            "wallet_name": "wallet",
            "maturity_status": "mature",
            "status": "draft",
        },
        credits=[
            {
                "id": 10,
                "credit_run_id": 1,
                "sc_node_id": "sc-2",
                "sc_node_display_name": "SC 2",
                "credit_amount": Decimal("121.875"),
                "credit_status": "draft",
            }
        ],
        address_lookup={
            "sc-2": [
                {
                    "sc_node_id": "sc-2",
                    "payout_address": "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv",
                }
            ]
        },
    )
    assert preview.plan_allowed is True
    assert preview.planned_amount_total == Decimal("121.875")
    assert preview.gross_planned_amount_total == Decimal("121.875")
    assert preview.correction_amount_total == Decimal("0")
    assert preview.payout_correction_id is None


def test_cycle3_catchup_net_payable_regression() -> None:
    preview = planner.build_payout_plan_preview(
        credit_run_id=5,
        wallet_name="wallet",
        reserve_fraction=Decimal("0.50"),
        trusted_balance_snapshot=Decimal("500"),
        credit_run={
            "id": 5,
            "wallet_name": "wallet",
            "maturity_status": "mature",
            "status": "draft",
        },
        credits=[
            {
                "id": 10,
                "credit_run_id": 5,
                "sc_node_id": "sc-2",
                "sc_node_display_name": "SC 2",
                "credit_amount": Decimal("61.875000000000"),
                "credit_status": "draft",
            }
        ],
        address_lookup={
            "sc-2": [
                {
                    "sc_node_id": "sc-2",
                    "payout_address": "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv",
                }
            ]
        },
        correction=_correction(
            amount="1.875000000000",
            credit_run_id=5,
        ),
    )
    assert preview.planned_amount_total == Decimal("60.000000000000")


def test_insert_sql_touches_only_correction_table() -> None:
    sql = correction.build_insert_correction_sql()
    tables = set(re.findall(r"insert\s+into\s+([a-z0-9_]+)", sql.lower()))
    assert tables == {"sc_node_payout_corrections"}


def test_readonly_correction_sql_is_select_only() -> None:
    for sql in (
        correction.build_corrections_list_sql(),
        correction.build_correction_details_sql(1),
    ):
        assert _MUTATING_SQL.search(sql) is None


def test_implementation_files_have_no_wallet_send_keywords() -> None:
    guard_block = re.compile(
        r"_WALLET_SEND_KEYWORDS = re\.compile\([\s\S]*?\)\n",
        re.MULTILINE,
    )
    for rel in (
        "payouts/collector/app/sc_node_payout_correction.py",
        "payouts/scripts/sc_node_payout_correction.py",
    ):
        text = (AZPOOL_ROOT / rel).read_text(encoding="utf-8")
        scrubbed = guard_block.sub("", text, count=1)
        assert _WALLET_SEND_KEYWORDS.search(scrubbed) is None


def test_correction_script_has_no_subprocess_or_shell_true() -> None:
    source = (
        AZPOOL_ROOT / "payouts/scripts/sc_node_payout_correction.py"
    ).read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "shell=True" not in source


def test_planner_script_preview_has_no_wallet_rpc() -> None:
    source = (AZPOOL_ROOT / "payouts/scripts/sc_node_payout_planner.py").read_text(
        encoding="utf-8"
    )
    preview_block = source.split("def _cmd_preview")[1].split("def _cmd_write_draft")[0]
    assert "subprocess" not in preview_block
    assert "sendtoaddress" not in preview_block


def test_production_executor_uses_plan_row_payout_amount_as_net() -> None:
    source = (
        AZPOOL_ROOT
        / "payouts/collector/app/sc_node_payout_production_executor.py"
    ).read_text(encoding="utf-8")
    assert 'row.get("payout_amount")' in source
    assert "build_sendtoaddress_argv" in source
