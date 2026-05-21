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
from payouts.collector.app import sc_node_payout_test_executor as executor


_WALLET_SEND_KEYWORDS = re.compile(
    r"\b("
    r"sendmany|sendtoaddress|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|privkey"
    r")\b",
    re.IGNORECASE,
)

_PLAN_ID = 1
_IDEMPOTENCY = "test-run-1"


def _approved_plan(*, preflight: str = "allowed") -> dict[str, object]:
    return {
        "id": _PLAN_ID,
        "status": plan_review.PLAN_STATUS_APPROVED,
        "preflight_status": preflight,
        "planned_amount_total": Decimal("121.875"),
        "wallet_name": "wallet",
    }


def _draft_plan() -> dict[str, object]:
    plan = _approved_plan()
    plan["status"] = plan_review.PLAN_STATUS_DRAFT
    plan["preflight_status"] = None
    return plan


def _plan_row(*, row_status: str = plan_review.ROW_STATUS_APPROVED) -> dict[str, object]:
    return {
        "id": 10,
        "payout_plan_id": _PLAN_ID,
        "credit_id": 1,
        "sc_node_id": "sc-2",
        "payout_address": "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv",
        "payout_amount": Decimal("121.875"),
        "row_status": row_status,
    }


def _sent_execution(*, idempotency_key: str = _IDEMPOTENCY) -> dict[str, object]:
    return {
        "id": 99,
        "payout_plan_id": _PLAN_ID,
        "mode": executor.EXECUTION_MODE_FAKE_REGTEST,
        "status": executor.EXECUTION_STATUS_SENT,
        "planned_amount_total": Decimal("121.875"),
        "test_wallet_name": "fake-regtest-wallet",
        "txid": "fake-regtest-1-abc",
        "execution_attempt_count": 1,
        "idempotency_key": idempotency_key,
        "notes": None,
        "created_at": None,
        "updated_at": None,
    }


def test_preview_succeeds_for_approved_preflight_allowed_plan() -> None:
    preview = executor.build_test_execution_preview(
        payout_plan_id=_PLAN_ID,
        mode=executor.EXECUTION_MODE_FAKE_REGTEST,
        test_wallet_name="fake-regtest-wallet",
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
    )
    assert preview.execution_allowed is True
    assert preview.refusal_reason is None
    assert preview.row_count == 1


def test_preview_refuses_non_approved_plan() -> None:
    preview = executor.build_test_execution_preview(
        payout_plan_id=_PLAN_ID,
        mode=executor.EXECUTION_MODE_FAKE_REGTEST,
        test_wallet_name="fake-regtest-wallet",
        plan=_draft_plan(),
        plan_rows=[_plan_row(row_status=plan_review.ROW_STATUS_DRAFT)],
    )
    assert preview.execution_allowed is False
    assert preview.refusal_reason is not None
    assert "approved" in preview.refusal_reason


def test_preview_refuses_approved_plan_without_allowed_preflight() -> None:
    preview = executor.build_test_execution_preview(
        payout_plan_id=_PLAN_ID,
        mode=executor.EXECUTION_MODE_FAKE_REGTEST,
        test_wallet_name="fake-regtest-wallet",
        plan=_approved_plan(preflight="refused"),
        plan_rows=[_plan_row()],
    )
    assert preview.execution_allowed is False
    assert preview.refusal_reason is not None
    assert "preflight_status" in preview.refusal_reason


def test_execute_fake_succeeds_and_generates_fake_txid() -> None:
    refusal = executor.evaluate_execute_fake_refusal(
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
        mode=executor.EXECUTION_MODE_FAKE_REGTEST,
        test_wallet_name="fake-regtest-wallet",
        existing_by_key=None,
        active_execution=None,
        idempotency_key=_IDEMPOTENCY,
    )
    assert refusal is None
    txid = executor.generate_fake_txid(
        payout_plan_id=_PLAN_ID,
        idempotency_key=_IDEMPOTENCY,
        payout_plan_row_ids=[10],
    )
    assert txid.startswith(f"fake-regtest-{_PLAN_ID}-")


def test_execute_fake_idempotent_for_same_plan_and_idempotency_key() -> None:
    refusal = executor.evaluate_execute_fake_refusal(
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
        mode=executor.EXECUTION_MODE_FAKE_REGTEST,
        test_wallet_name="fake-regtest-wallet",
        existing_by_key=_sent_execution(),
        active_execution=_sent_execution(),
        idempotency_key=_IDEMPOTENCY,
    )
    assert refusal is None


def test_execute_fake_refuses_duplicate_active_execution_with_different_idempotency_key() -> None:
    refusal = executor.evaluate_execute_fake_refusal(
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
        mode=executor.EXECUTION_MODE_FAKE_REGTEST,
        test_wallet_name="fake-regtest-wallet",
        existing_by_key=None,
        active_execution=_sent_execution(idempotency_key="first-key"),
        idempotency_key="second-key",
    )
    assert refusal is not None
    assert "active test execution already exists" in refusal


def test_execute_fake_requires_fake_regtest_mode() -> None:
    refusal = executor.evaluate_execute_fake_refusal(
        plan=_approved_plan(),
        plan_rows=[_plan_row()],
        mode=executor.EXECUTION_MODE_REGTEST,
        test_wallet_name="fake-regtest-wallet",
        existing_by_key=None,
        active_execution=None,
        idempotency_key=_IDEMPOTENCY,
    )
    assert refusal is not None
    assert "fake_regtest" in refusal


def test_mark_confirmed_transitions_sent_to_confirmed() -> None:
    refusal = executor.evaluate_mark_confirmed_refusal(_sent_execution())
    assert refusal is None


def test_mark_confirmed_idempotent_if_already_confirmed() -> None:
    confirmed = dict(_sent_execution())
    confirmed["status"] = executor.EXECUTION_STATUS_CONFIRMED
    refusal = executor.evaluate_mark_confirmed_refusal(confirmed)
    assert refusal is None


def test_mark_confirmed_refuses_failed_execution() -> None:
    failed = dict(_sent_execution())
    failed["status"] = executor.EXECUTION_STATUS_FAILED
    refusal = executor.evaluate_mark_confirmed_refusal(failed)
    assert refusal is not None
    assert "failed" in refusal


def test_normalize_test_wallet_name_blocks_production_wallet() -> None:
    with pytest.raises(ValueError, match="production"):
        executor.normalize_test_wallet_name("wallet")


def test_readonly_admin_sql_for_test_executions() -> None:
    list_sql = admin_readonly.build_payout_test_executions_sql()
    details_sql = admin_readonly.build_payout_test_execution_details_sql(1)
    rows_sql = admin_readonly.build_payout_test_execution_rows_sql(1)
    for sql in (list_sql, details_sql, rows_sql):
        admin_readonly.assert_readonly_sql(sql)
    assert "sc_node_payout_test_executions" in list_sql
    assert "sc_node_payout_test_execution_rows" in rows_sql


def test_admin_row_serializers_for_test_execution() -> None:
    header = executor.row_to_test_execution_dict(_sent_execution())
    assert header["mode"] == executor.EXECUTION_MODE_FAKE_REGTEST
    row = executor.row_to_test_execution_row_dict(
        {
            "id": 1,
            "test_execution_id": 99,
            "payout_plan_row_id": 10,
            "sc_node_id": "sc-2",
            "payout_address": "az1test",
            "payout_amount": Decimal("1"),
            "row_status": executor.ROW_STATUS_SENT,
            "txid": "fake-regtest-1-abc",
            "created_at": None,
            "updated_at": None,
        }
    )
    assert row["row_status"] == executor.ROW_STATUS_SENT


def test_test_insert_sql_touches_only_test_execution_tables() -> None:
    for builder in (
        executor.build_insert_test_execution_sql,
        executor.build_insert_test_execution_row_sql,
        executor.build_update_execution_confirmed_sql,
        executor.build_update_execution_rows_confirmed_sql,
    ):
        sql = builder()
        tables = set(
            re.findall(r"\b(?:insert\s+into|update)\s+([a-z0-9_]+)\b", sql.lower())
        )
        assert tables <= {
            "sc_node_payout_test_executions",
            "sc_node_payout_test_execution_rows",
        }


def test_implementation_files_have_no_wallet_send_keywords() -> None:
    guard_block = re.compile(
        r"_WALLET_SEND_KEYWORDS = re\.compile\([\s\S]*?\)\n",
        re.MULTILINE,
    )
    for rel in (
        "payouts/collector/app/sc_node_payout_test_executor.py",
        "payouts/scripts/sc_node_payout_test_executor.py",
    ):
        text = (AZPOOL_ROOT / rel).read_text(encoding="utf-8")
        scrubbed = guard_block.sub("", text, count=1)
        assert _WALLET_SEND_KEYWORDS.search(scrubbed) is None


def test_script_has_no_subprocess_or_shell_true() -> None:
    source = (AZPOOL_ROOT / "payouts/scripts/sc_node_payout_test_executor.py").read_text(
        encoding="utf-8"
    )
    assert "subprocess" not in source
    assert "shell=True" not in source


def test_script_argparse_includes_required_commands() -> None:
    from payouts.scripts import sc_node_payout_test_executor as cli

    args = cli._parse_args(
        [
            "preview",
            "--payout-plan-id",
            "1",
            "--mode",
            "fake_regtest",
            "--test-wallet-name",
            "fake-regtest-wallet",
        ]
    )
    assert args.command == "preview"
    assert args.payout_plan_id == 1
    assert args.mode == "fake_regtest"
