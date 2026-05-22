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
from payouts.collector.app import sc_node_payout_production_executor as executor
from payouts.collector.app import sc_node_payout_production_preflight as production_preflight


_FORBIDDEN_RPC = re.compile(
    r"\b("
    r"sendmany|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|privkey"
    r")\b",
    re.IGNORECASE,
)

_PLAN_ID = 1
_PREFLIGHT_ID = 1
_SOURCE_WALLET = "wallet"
_PLANNED = Decimal("121.875")
_CONFIRM = "SEND 121.875000000000 FROM wallet FOR PLAN 1"
_IDEMPOTENCY = "production-real-v0-plan-1"


def _getbalances_payload(trusted: str = "664.37481345") -> dict[str, object]:
    return {"mine": {"trusted": trusted, "immature": "0"}}


def _approved_plan() -> dict[str, object]:
    return {
        "id": _PLAN_ID,
        "status": plan_review.PLAN_STATUS_APPROVED,
        "planned_amount_total": _PLANNED,
        "wallet_name": _SOURCE_WALLET,
    }


def _passed_preflight() -> dict[str, object]:
    return {
        "id": _PREFLIGHT_ID,
        "payout_plan_id": _PLAN_ID,
        "source_wallet_name": _SOURCE_WALLET,
        "preflight_status": production_preflight.PREFLIGHT_STATUS_PASSED,
        "execution_allowed": True,
        "planned_amount_total": _PLANNED,
    }


def _plan_row() -> dict[str, object]:
    return {
        "id": 10,
        "payout_plan_id": _PLAN_ID,
        "sc_node_id": "sc-2",
        "payout_address": "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv",
        "payout_amount": _PLANNED,
        "row_status": plan_review.ROW_STATUS_APPROVED,
    }


def _preflight_row() -> dict[str, object]:
    return {
        "payout_plan_row_id": 10,
        "sc_node_id": "sc-2",
        "payout_address": "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv",
        "payout_amount": _PLANNED,
        "row_status": production_preflight.ROW_STATUS_CHECKED,
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


def _wallet_balance(trusted: str = "664.37481345") -> executor.WalletBalance:
    return executor.parse_wallet_balance_from_getbalances(_getbalances_payload(trusted))


def test_build_expected_confirmation_phrase_exact_format() -> None:
    phrase = executor.build_expected_confirmation_phrase(
        _PLAN_ID,
        _PLANNED,
        _SOURCE_WALLET,
    )
    assert phrase == _CONFIRM


def test_execute_real_refuses_wrong_confirmation_phrase() -> None:
    refusal = executor.evaluate_execute_real_refusal(
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
        preflight=_passed_preflight(),
        preflight_rows=[_preflight_row()],
        source_wallet_name=_SOURCE_WALLET,
        wallet_balance=_wallet_balance(),
        address_lookup=_address_lookup(),
        confirmation_phrase="SEND 1 FROM wallet FOR PLAN 1",
        existing_by_key=None,
        active_execution=None,
        idempotency_key=_IDEMPOTENCY,
    )
    assert refusal is not None
    assert "confirmation phrase mismatch" in refusal


def test_preview_allows_single_row_without_allow_multiple_flag() -> None:
    preview = executor.build_production_execution_preview(
        payout_plan_id=_PLAN_ID,
        production_preflight_id=_PREFLIGHT_ID,
        source_wallet_name=_SOURCE_WALLET,
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
        preflight=_passed_preflight(),
        preflight_rows=[_preflight_row()],
        wallet_balance=_wallet_balance(),
        address_lookup=_address_lookup(),
    )
    assert preview.execution_allowed is True


def test_execute_real_refuses_multiple_rows_without_flag() -> None:
    row = _plan_row()
    row2 = dict(row)
    row2["id"] = 11
    preflight2 = dict(_preflight_row())
    preflight2["payout_plan_row_id"] = 11
    refusal = executor.evaluate_execute_real_refusal(
        plan=_approved_plan(),
        plan_rows=[row, row2],
        preflight=_passed_preflight(),
        preflight_rows=[_preflight_row(), preflight2],
        source_wallet_name=_SOURCE_WALLET,
        wallet_balance=_wallet_balance("1000"),
        address_lookup=_address_lookup(),
        confirmation_phrase=_CONFIRM,
        existing_by_key=None,
        active_execution=None,
        idempotency_key=_IDEMPOTENCY,
        allow_multiple_rows=False,
    )
    assert refusal is not None
    assert "allow-multiple-rows" in refusal


def test_planned_amount_above_spendable_after_reserve_refuses() -> None:
    refusal = executor.evaluate_preview_refusal(
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
        preflight=_passed_preflight(),
        preflight_rows=[_preflight_row()],
        source_wallet_name=_SOURCE_WALLET,
        wallet_balance=_wallet_balance("200"),
        address_lookup=_address_lookup(),
    )
    assert refusal is not None
    assert "spendable_after_reserve" in refusal


def test_planned_amount_above_trusted_balance_refuses() -> None:
    refusal = executor.evaluate_preview_refusal(
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
        preflight=_passed_preflight(),
        preflight_rows=[_preflight_row()],
        source_wallet_name=_SOURCE_WALLET,
        wallet_balance=_wallet_balance("100"),
        address_lookup=_address_lookup(),
    )
    assert refusal is not None
    assert "trusted wallet balance" in refusal


def test_preview_refuses_without_passed_preflight() -> None:
    preflight = _passed_preflight()
    preflight["preflight_status"] = production_preflight.PREFLIGHT_STATUS_REFUSED
    preflight["execution_allowed"] = False
    refusal = executor.evaluate_preview_refusal(
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
        preflight=None,
        preflight_rows=[_preflight_row()],
        source_wallet_name=_SOURCE_WALLET,
        wallet_balance=_wallet_balance(),
        address_lookup=_address_lookup(),
    )
    assert refusal is not None
    assert "preflight" in refusal


def test_preview_refuses_non_approved_plan() -> None:
    plan = _approved_plan()
    plan["status"] = plan_review.PLAN_STATUS_DRAFT
    refusal = executor.evaluate_preview_refusal(
        plan=plan,
        plan_rows=[_plan_row()],
        preflight=_passed_preflight(),
        preflight_rows=[_preflight_row()],
        source_wallet_name=_SOURCE_WALLET,
        wallet_balance=_wallet_balance(),
        address_lookup=_address_lookup(),
    )
    assert refusal is not None
    assert "approved" in refusal


def test_preview_refuses_address_drift() -> None:
    row = _plan_row()
    row["payout_address"] = "az1plan"
    preflight_row = _preflight_row()
    preflight_row["payout_address"] = "az1plan"
    refusal = executor.evaluate_preview_refusal(
        plan=_approved_plan(),
        plan_rows=[row],
        preflight=_passed_preflight(),
        preflight_rows=[preflight_row],
        source_wallet_name=_SOURCE_WALLET,
        wallet_balance=_wallet_balance(),
        address_lookup=_address_lookup(address="az1registry"),
    )
    assert refusal is not None
    assert "drift" in refusal


def test_active_execution_with_different_idempotency_key_refuses() -> None:
    refusal = executor.evaluate_execute_real_refusal(
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
        preflight=_passed_preflight(),
        preflight_rows=[_preflight_row()],
        source_wallet_name=_SOURCE_WALLET,
        wallet_balance=_wallet_balance(),
        address_lookup=_address_lookup(),
        confirmation_phrase=_CONFIRM,
        existing_by_key=None,
        active_execution={
            "id": 5,
            "idempotency_key": "other-key",
            "status": executor.EXECUTION_STATUS_SENT,
        },
        idempotency_key=_IDEMPOTENCY,
    )
    assert refusal is not None
    assert "active production execution" in refusal


def test_idempotent_replay_returns_none_refusal_when_existing_by_key() -> None:
    refusal = executor.evaluate_execute_real_refusal(
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
        preflight=_passed_preflight(),
        preflight_rows=[_preflight_row()],
        source_wallet_name=_SOURCE_WALLET,
        wallet_balance=_wallet_balance(),
        address_lookup=_address_lookup(),
        confirmation_phrase=_CONFIRM,
        existing_by_key={"id": 1, "idempotency_key": _IDEMPOTENCY},
        active_execution=None,
        idempotency_key=_IDEMPOTENCY,
    )
    assert refusal is None


def test_sendtoaddress_argv_is_explicit_list() -> None:
    argv = executor.build_sendtoaddress_argv(
        azc_bin="/tmp/azc",
        source_wallet_name=_SOURCE_WALLET,
        payout_address="az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv",
        payout_amount=_PLANNED,
    )
    assert argv == [
        "/tmp/azc",
        "-rpcwallet=wallet",
        "sendtoaddress",
        "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv",
        "121.875000000000",
    ]


def test_script_preview_has_no_sendtoaddress() -> None:
    source = (
        AZPOOL_ROOT / "payouts/scripts/sc_node_payout_production_executor.py"
    ).read_text(encoding="utf-8")
    preview_block = source.split("def _cmd_preview")[1].split("def _cmd_execute_real")[0]
    assert "sendtoaddress" not in preview_block


def test_script_subprocess_run_does_not_use_shell_true() -> None:
    source = (
        AZPOOL_ROOT / "payouts/scripts/sc_node_payout_production_executor.py"
    ).read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "subprocess.run" in source


def test_script_execute_real_uses_sendtoaddress() -> None:
    source = (
        AZPOOL_ROOT / "payouts/scripts/sc_node_payout_production_executor.py"
    ).read_text(encoding="utf-8")
    assert "_run_sendtoaddress" in source
    assert "execute-real" in source


def test_app_module_has_no_forbidden_wallet_rpcs_except_sendtoaddress_path() -> None:
    path = AZPOOL_ROOT / "payouts/collector/app/sc_node_payout_production_executor.py"
    text = path.read_text(encoding="utf-8")
    guard_block = re.compile(
        r"_FORBIDDEN_WALLET_RPC_KEYWORDS = re\.compile\([\s\S]*?\)\n",
        re.MULTILINE,
    )
    send_block = re.search(
        r"def build_sendtoaddress_argv[\s\S]*?return argv\n",
        text,
    )
    scrubbed = guard_block.sub("", text, count=1)
    if send_block:
        scrubbed = scrubbed.replace(send_block.group(0), "")
    assert _FORBIDDEN_RPC.search(scrubbed) is None


def test_insert_sql_touches_only_production_execution_tables() -> None:
    for builder in (
        executor.build_insert_production_execution_sql,
        executor.build_insert_production_execution_row_sql,
        executor.build_mark_production_execution_sent_sql,
        executor.build_mark_production_execution_row_sent_sql,
    ):
        sql = builder()
        tables = set(
            re.findall(r"\b(?:insert\s+into|update)\s+([a-z0-9_]+)\b", sql.lower())
        )
        assert tables <= {
            "sc_node_payout_production_executions",
            "sc_node_payout_production_execution_rows",
        }


def test_no_payout_plan_mutation_sql() -> None:
    module_source = (
        AZPOOL_ROOT / "payouts/collector/app/sc_node_payout_production_executor.py"
    ).read_text(encoding="utf-8")
    assert "update sc_node_payout_plans" not in module_source.lower()
    for sql in (
        executor.build_production_executions_sql(),
        executor.build_production_execution_details_sql(1),
        executor.build_approved_payout_plan_for_execution_sql(1),
    ):
        assert "update sc_node_payout_plans" not in sql.lower()


def test_admin_production_executions_sql_is_select_only() -> None:
    sql = admin_readonly.build_production_executions_sql()
    assert "sc_node_payout_production_executions" in sql
    admin_readonly.assert_readonly_sql(sql)


def test_calculate_execution_guardrails_default_fifty_percent_reserve() -> None:
    trusted = Decimal("664.37481345")
    result = executor.calculate_execution_guardrails(
        trusted_balance=trusted,
        planned_amount_total=_PLANNED,
    )
    assert result["reserve_amount"] == Decimal("332.187406725000")
    assert result["spendable_after_reserve"] == Decimal("332.187406725000")
