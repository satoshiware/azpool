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
from payouts.collector.app import sc_node_payout_plan_review as plan_review
from payouts.collector.app import sc_node_payout_production_preflight as production


_WALLET_SEND_KEYWORDS = re.compile(
    r"\b("
    r"sendmany|sendtoaddress|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|privkey"
    r")\b",
    re.IGNORECASE,
)

_PLAN_ID = 1


def _getbalances_payload(
    *,
    trusted: str = "660.62481345",
    immature: str = "7.50000000",
) -> dict[str, object]:
    return {"mine": {"trusted": trusted, "immature": immature}}


def _approved_plan(*, planned: str = "121.875") -> dict[str, object]:
    return {
        "id": _PLAN_ID,
        "status": plan_review.PLAN_STATUS_APPROVED,
        "planned_amount_total": Decimal(planned),
        "wallet_name": "wallet",
    }


def _plan_row(
    *,
    row_id: int = 10,
    address: str = "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv",
) -> dict[str, object]:
    return {
        "id": row_id,
        "payout_plan_id": _PLAN_ID,
        "credit_id": 1,
        "sc_node_id": "sc-2",
        "payout_address": address,
        "payout_amount": Decimal("121.875"),
        "row_status": plan_review.ROW_STATUS_APPROVED,
    }


def _address_lookup(
    address: str = "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv",
) -> dict[str, list[dict[str, object]]]:
    return {
        "sc-2": [
            {
                "sc_node_id": "sc-2",
                "payout_address": address,
                "status": "active",
                "is_default": True,
            }
        ]
    }


def test_parse_wallet_balance_from_getbalances_reads_mine_trusted_and_immature() -> None:
    balance = production.parse_wallet_balance_from_getbalances(_getbalances_payload())
    assert balance.trusted == Decimal("660.624813450000")
    assert balance.immature == Decimal("7.500000000000")


def test_calculate_reserve_defaults_to_fifty_percent() -> None:
    trusted = Decimal("660.62481345")
    result = production.calculate_reserve(trusted)
    assert result["reserve_percent"] == Decimal("0.5")
    assert result["reserve_amount"] == Decimal("330.312406725000")
    assert result["spendable_after_reserve"] == Decimal("330.312406725000")
    assert result["max_spend_allowed"] == Decimal("330.312406725000")


def test_planned_amount_within_spendable_after_reserve_allows() -> None:
    trusted = Decimal("660")
    reserve = production.calculate_reserve(trusted)
    preview = production.build_production_preflight_preview(
        payout_plan_id=_PLAN_ID,
        source_wallet_name="wallet",
        plan=_approved_plan(planned="300"),
        plan_rows=[_plan_row()],
        wallet_balance=production.WalletBalance(trusted=trusted, immature=Decimal("0")),
        address_lookup=_address_lookup(),
        reserve_percent=reserve["reserve_percent"],
        max_spend_percent=reserve["max_spend_percent"],
    )
    assert preview.execution_allowed is True


def test_planned_amount_above_spendable_after_reserve_refuses() -> None:
    trusted = Decimal("660")
    reserve = production.calculate_reserve(trusted)
    preview = production.build_production_preflight_preview(
        payout_plan_id=_PLAN_ID,
        source_wallet_name="wallet",
        plan=_approved_plan(planned="400"),
        plan_rows=[_plan_row()],
        wallet_balance=production.WalletBalance(trusted=trusted, immature=Decimal("0")),
        address_lookup=_address_lookup(),
        reserve_percent=reserve["reserve_percent"],
        max_spend_percent=reserve["max_spend_percent"],
    )
    assert preview.execution_allowed is False
    assert preview.refusal_reason is not None
    assert "spendable_after_reserve" in preview.refusal_reason


def test_override_reserve_records_operator_override_and_allows_above_reserve() -> None:
    trusted = Decimal("660")
    reserve = production.calculate_reserve(trusted)
    preview = production.build_production_preflight_preview(
        payout_plan_id=_PLAN_ID,
        source_wallet_name="wallet",
        plan=_approved_plan(planned="400"),
        plan_rows=[_plan_row()],
        wallet_balance=production.WalletBalance(trusted=trusted, immature=Decimal("0")),
        address_lookup=_address_lookup(),
        operator_override=True,
        reserve_percent=reserve["reserve_percent"],
        max_spend_percent=reserve["max_spend_percent"],
    )
    assert preview.operator_override is True
    assert preview.execution_allowed is True


def test_override_still_refuses_planned_amount_above_trusted_balance() -> None:
    trusted = Decimal("660")
    preview = production.build_production_preflight_preview(
        payout_plan_id=_PLAN_ID,
        source_wallet_name="wallet",
        plan=_approved_plan(planned="700"),
        plan_rows=[_plan_row()],
        wallet_balance=production.WalletBalance(trusted=trusted, immature=Decimal("0")),
        address_lookup=_address_lookup(),
        operator_override=True,
    )
    assert preview.execution_allowed is False
    assert preview.refusal_reason is not None
    assert "trusted wallet balance" in preview.refusal_reason


def test_preview_refuses_non_approved_plan() -> None:
    plan = _approved_plan()
    plan["status"] = plan_review.PLAN_STATUS_DRAFT
    preview = production.build_production_preflight_preview(
        payout_plan_id=_PLAN_ID,
        source_wallet_name="wallet",
        plan=plan,
        plan_rows=[_plan_row()],
        wallet_balance=production.WalletBalance(
            trusted=Decimal("660"), immature=Decimal("0")
        ),
        address_lookup=_address_lookup(),
    )
    assert preview.execution_allowed is False
    assert "approved" in (preview.refusal_reason or "")


def test_preview_refuses_address_drift() -> None:
    preview = production.build_production_preflight_preview(
        payout_plan_id=_PLAN_ID,
        source_wallet_name="wallet",
        plan=_approved_plan(),
        plan_rows=[_plan_row(address="az1plan")],
        wallet_balance=production.WalletBalance(
            trusted=Decimal("660"), immature=Decimal("0")
        ),
        address_lookup=_address_lookup(address="az1registry"),
    )
    assert preview.execution_allowed is False
    assert preview.refusal_reason is not None
    assert "drift" in preview.refusal_reason


def test_approved_plan_sql_requires_approved_status() -> None:
    sql = production.build_approved_payout_plan_sql(_PLAN_ID)
    assert "status = 'approved'" in sql
    production.assert_no_wallet_send_keywords(sql)


def test_approved_plan_rows_sql_requires_approved_row_status() -> None:
    sql = production.build_approved_payout_plan_rows_sql(_PLAN_ID)
    assert "row_status = 'approved'" in sql


def test_active_payout_address_join_exists_in_sql() -> None:
    sql = production.build_approved_payout_plan_rows_with_active_address_sql(_PLAN_ID)
    assert "sc_node_payout_addresses" in sql
    assert "is_default = true" in sql
    assert "status = 'active'" in sql


def test_insert_sql_touches_only_production_preflight_tables() -> None:
    for builder in (
        production.build_insert_production_preflight_sql,
        production.build_insert_production_preflight_row_sql,
    ):
        sql = builder()
        tables = set(re.findall(r"insert\s+into\s+([a-z0-9_]+)", sql.lower()))
        assert tables <= {
            "sc_node_payout_production_preflights",
            "sc_node_payout_production_preflight_rows",
        }


def test_no_production_payout_plan_mutation_sql() -> None:
    module_source = (
        AZPOOL_ROOT / "payouts/collector/app/sc_node_payout_production_preflight.py"
    ).read_text(encoding="utf-8")
    assert "update sc_node_payout_plans" not in module_source.lower()
    assert "update sc_node_payout_plan_rows" not in module_source.lower()
    for sql in (
        production.build_production_preflights_sql(),
        production.build_production_preflight_details_sql(1),
        production.build_approved_payout_plan_sql(1),
    ):
        assert "update sc_node_payout_plans" not in sql.lower()
        assert "update sc_node_payout_plan_rows" not in sql.lower()


def test_admin_sql_is_select_only() -> None:
    for sql in (
        admin_readonly.build_production_preflights_sql(),
        admin_readonly.build_production_preflight_details_sql(1),
        admin_readonly.build_production_preflight_rows_sql(1),
    ):
        admin_readonly.assert_readonly_sql(sql)


def test_script_getbalances_argv_is_explicit_list_without_shell() -> None:
    from payouts.scripts import sc_node_payout_production_preflight as cli

    argv = cli._getbalances_argv(azc_bin="/tmp/azc", source_wallet_name="wallet")
    assert argv == ["/tmp/azc", "-rpcwallet=wallet", "getbalances"]
    assert isinstance(argv, list)


def test_script_subprocess_run_does_not_use_shell_true() -> None:
    source = (
        AZPOOL_ROOT / "payouts/scripts/sc_node_payout_production_preflight.py"
    ).read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "subprocess.run" in source
    assert "getbalances" in source


def test_implementation_files_have_no_wallet_send_keywords() -> None:
    guard_block = re.compile(
        r"_WALLET_SEND_KEYWORDS = re\.compile\([\s\S]*?\)\n",
        re.MULTILINE,
    )
    for rel in (
        "payouts/collector/app/sc_node_payout_production_preflight.py",
        "payouts/scripts/sc_node_payout_production_preflight.py",
    ):
        text = (AZPOOL_ROOT / rel).read_text(encoding="utf-8")
        scrubbed = guard_block.sub("", text, count=1)
        assert _WALLET_SEND_KEYWORDS.search(scrubbed) is None


def test_script_has_no_send_rpc_calls() -> None:
    source = (
        AZPOOL_ROOT / "payouts/scripts/sc_node_payout_production_preflight.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "sendtoaddress",
        "sendmany",
        "sendrawtransaction",
        "signrawtransaction",
        "createrawtransaction",
        "listtransactions",
    ):
        assert forbidden not in source
