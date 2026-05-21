from __future__ import annotations

import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

from payouts.collector.app import sc_node_payout_plan_review as review


_WALLET_SEND_KEYWORDS = re.compile(
    r"\b("
    r"sendmany|sendtoaddress|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|privkey"
    r")\b",
    re.IGNORECASE,
)

_PLAN_ID = 1


def _draft_plan(*, planned: str = "121.875", max_spend: str = "330.312406725") -> dict[str, object]:
    return {
        "id": _PLAN_ID,
        "status": "draft",
        "row_count": 1,
        "planned_amount_total": Decimal(planned),
        "max_spendable_amount": Decimal(max_spend),
        "reserve_fraction": Decimal("0.5"),
    }


def _approved_plan() -> dict[str, object]:
    plan = _draft_plan()
    plan["status"] = "approved"
    return plan


def _plan_row(
    *,
    address: str = "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv",
    row_status: str = "draft",
) -> dict[str, object]:
    return {
        "id": 1,
        "payout_plan_id": _PLAN_ID,
        "credit_id": 10,
        "sc_node_id": "sc-2",
        "sc_node_display_name": "SC Node 2",
        "payout_address": address,
        "row_status": row_status,
        "payout_amount": Decimal("121.875"),
        "created_at": None,
        "updated_at": None,
    }


def _address_lookup(address: str = "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv") -> dict[str, list[dict[str, object]]]:
    return {
        "sc-2": [
            {
                "sc_node_id": "sc-2",
                "payout_address": address,
                "sc_node_display_name": "SC Node 2",
            }
        ]
    }


def test_approve_succeeds_with_exact_confirmation() -> None:
    phrase = review.build_approval_confirmation_phrase(_PLAN_ID)
    refusal = review.evaluate_approve_refusal(
        plan=_draft_plan(),
        plan_rows=[_plan_row()],
        address_lookup=_address_lookup(),
        confirmation=phrase,
        payout_plan_id=_PLAN_ID,
    )
    assert refusal is None
    assert review.verify_approval_confirmation(phrase, _PLAN_ID)


def test_approve_refuses_wrong_confirmation() -> None:
    refusal = review.evaluate_approve_refusal(
        plan=_draft_plan(),
        plan_rows=[_plan_row()],
        address_lookup=_address_lookup(),
        confirmation="APPROVE PAYOUT PLAN 1 SEND",
        payout_plan_id=_PLAN_ID,
    )
    assert refusal is not None
    assert "confirmation must be exactly" in refusal


def test_approve_refuses_plan_over_max_spendable() -> None:
    refusal = review.evaluate_approve_refusal(
        plan=_draft_plan(planned="500", max_spend="100"),
        plan_rows=[_plan_row()],
        address_lookup=_address_lookup(),
        confirmation=review.build_approval_confirmation_phrase(_PLAN_ID),
        payout_plan_id=_PLAN_ID,
    )
    assert refusal is not None
    assert "max_spendable_amount" in refusal


def test_approve_refuses_address_drift() -> None:
    refusal = review.evaluate_approve_refusal(
        plan=_draft_plan(),
        plan_rows=[_plan_row(address="az1frozen")],
        address_lookup=_address_lookup(address="az1registry"),
        confirmation=review.build_approval_confirmation_phrase(_PLAN_ID),
        payout_plan_id=_PLAN_ID,
    )
    assert refusal is not None
    assert "address drift" in refusal


def test_cancel_succeeds_from_draft() -> None:
    refusal = review.evaluate_cancel_refusal(plan=_draft_plan(), reason="operator hold")
    assert refusal is None


def test_cancel_succeeds_from_approved() -> None:
    refusal = review.evaluate_cancel_refusal(plan=_approved_plan(), reason="revised plan")
    assert refusal is None


def test_cancel_refuses_cancelled_plan() -> None:
    plan = _draft_plan()
    plan["status"] = "cancelled"
    refusal = review.evaluate_cancel_refusal(plan=plan, reason="too late")
    assert refusal is not None


def test_preflight_succeeds_for_approved_plan_with_sufficient_balance() -> None:
    result = review.build_preflight_result(
        payout_plan_id=_PLAN_ID,
        plan=_approved_plan(),
        plan_rows=[_plan_row(row_status="approved")],
        trusted_balance_current=Decimal("660.624813450000"),
        reserve_fraction_current=Decimal("0.5"),
        address_lookup=_address_lookup(),
    )
    assert result.preflight_allowed is True
    payload = review.preflight_result_to_dict(result)
    assert payload["preflight_allowed"] is True
    assert "no-send preflight" in payload["accounting_note"]


def test_preflight_refuses_non_approved_plan() -> None:
    refusal = review.evaluate_preflight_refusal(
        plan=_draft_plan(),
        plan_rows=[_plan_row()],
        address_lookup=_address_lookup(),
        trusted_balance_current=Decimal("660"),
        reserve_fraction_current=Decimal("0.5"),
    )
    assert refusal is not None
    assert "approved" in refusal


def test_preflight_refuses_insufficient_current_trusted_balance() -> None:
    refusal = review.evaluate_preflight_refusal(
        plan=_approved_plan(),
        plan_rows=[_plan_row(row_status="approved")],
        address_lookup=_address_lookup(),
        trusted_balance_current=Decimal("100"),
        reserve_fraction_current=Decimal("0.5"),
    )
    assert refusal is not None
    assert "max_spendable_amount" in refusal


def test_preflight_refuses_address_drift() -> None:
    refusal = review.evaluate_preflight_refusal(
        plan=_approved_plan(),
        plan_rows=[_plan_row(address="az1frozen", row_status="approved")],
        address_lookup=_address_lookup(address="az1current"),
        trusted_balance_current=Decimal("660"),
        reserve_fraction_current=Decimal("0.5"),
    )
    assert refusal is not None
    assert "address drift" in refusal


def test_readonly_details_sql_includes_review_metadata() -> None:
    sql = review.build_payout_plan_details_sql(_PLAN_ID)
    assert "approved_at" in sql
    assert "preflight_status" in sql
    assert "cancellation_note" in sql


def test_row_to_payout_plan_dict_includes_review_metadata() -> None:
    result = review.row_to_payout_plan_dict(
        {
            "id": 1,
            "credit_run_id": 1,
            "wallet_name": "wallet",
            "status": "approved",
            "reserve_fraction": Decimal("0.5"),
            "trusted_balance_snapshot": Decimal("660"),
            "reserve_amount": Decimal("330"),
            "max_spendable_amount": Decimal("330"),
            "planned_amount_total": Decimal("121.875"),
            "row_count": 1,
            "notes": None,
            "approved_at": None,
            "approved_by": "ops",
            "approval_note": "ok",
            "approval_confirmation_hash": "abc",
            "preflight_checked_at": None,
            "preflight_status": "allowed",
            "preflight_note": None,
            "cancelled_at": None,
            "cancelled_by": None,
            "cancellation_note": None,
            "created_at": None,
            "updated_at": None,
        }
    )
    assert result["approved_by"] == "ops"
    assert result["preflight_status"] == "allowed"
    assert result["approval_confirmation_hash"] == "abc"


def test_review_update_sql_touches_only_plan_tables() -> None:
    for builder in (
        review.build_update_approve_plan_sql,
        review.build_update_approve_rows_sql,
        review.build_update_cancel_plan_sql,
        review.build_update_cancel_rows_sql,
        review.build_update_preflight_plan_sql,
    ):
        sql = builder()
        tables = set(re.findall(r"\bupdate\s+([a-z0-9_]+)\b", sql.lower()))
        assert tables <= {"sc_node_payout_plans", "sc_node_payout_plan_rows"}


def test_implementation_files_have_no_wallet_send_keywords() -> None:
    guard_block = re.compile(
        r"_WALLET_SEND_KEYWORDS = re\.compile\([\s\S]*?\)\n",
        re.MULTILINE,
    )
    for rel in (
        "payouts/collector/app/sc_node_payout_plan_review.py",
        "payouts/scripts/sc_node_payout_plan_review.py",
    ):
        text = (AZPOOL_ROOT / rel).read_text(encoding="utf-8")
        scrubbed = guard_block.sub("", text, count=1)
        assert _WALLET_SEND_KEYWORDS.search(scrubbed) is None


def test_script_has_no_subprocess_or_shell_true() -> None:
    source = (AZPOOL_ROOT / "payouts/scripts/sc_node_payout_plan_review.py").read_text(
        encoding="utf-8"
    )
    assert "subprocess" not in source
    assert "shell=True" not in source
