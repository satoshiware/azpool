from __future__ import annotations

import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

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


def _credit_run() -> dict[str, object]:
    return {
        "id": 1,
        "wallet_name": "wallet",
        "maturity_status": "mature",
        "status": "draft",
        "reward_amount_total": Decimal("121.875"),
    }


def _credit(
    *,
    credit_id: int = 10,
    sc_node_id: str = "sc-2",
    amount: str = "121.875",
) -> dict[str, object]:
    return {
        "id": credit_id,
        "credit_run_id": 1,
        "sc_node_id": sc_node_id,
        "sc_node_display_name": "SC Node 2",
        "credit_amount": Decimal(amount),
        "credit_status": "draft",
    }


def _address(sc_node_id: str = "sc-2", address: str = "az1qxgr54ykergmzp7h7fg37lgtc0ccdce355xppqv") -> dict[str, object]:
    return {
        "sc_node_id": sc_node_id,
        "payout_address": address,
        "sc_node_display_name": "SC Node 2",
    }


def test_preview_succeeds_with_sufficient_trusted_balance() -> None:
    preview = planner.build_payout_plan_preview(
        credit_run_id=1,
        wallet_name="wallet",
        reserve_fraction=Decimal("0.50"),
        trusted_balance_snapshot=Decimal("300"),
        credit_run=_credit_run(),
        credits=[_credit()],
        address_lookup={"sc-2": [_address()]},
    )
    assert preview.plan_allowed is True
    assert preview.planned_amount_total == Decimal("121.875")
    assert preview.gross_planned_amount_total == Decimal("121.875")
    assert preview.correction_amount_total == Decimal("0")
    assert preview.payout_correction_id is None
    payload = planner.payout_plan_preview_to_dict(preview)
    assert payload["row_count"] == 1
    assert payload["reserve_percent"] == "50.00"
    assert payload["correction_amount_total"] == "0"
    assert preview.max_spendable_amount == Decimal("150")


def test_preview_refuses_when_planned_exceeds_max_spendable() -> None:
    preview = planner.build_payout_plan_preview(
        credit_run_id=1,
        wallet_name="wallet",
        reserve_fraction=Decimal("0.50"),
        trusted_balance_snapshot=Decimal("100"),
        credit_run=_credit_run(),
        credits=[_credit()],
        address_lookup={"sc-2": [_address()]},
    )
    assert preview.plan_allowed is False
    assert preview.refusal_reason is not None
    assert "max_spendable_amount" in preview.refusal_reason


def test_preview_refuses_missing_active_default_address() -> None:
    preview = planner.build_payout_plan_preview(
        credit_run_id=1,
        wallet_name="wallet",
        reserve_fraction=Decimal("0.50"),
        trusted_balance_snapshot=Decimal("300"),
        credit_run=_credit_run(),
        credits=[_credit()],
        address_lookup={"sc-2": []},
    )
    assert preview.plan_allowed is False
    assert preview.refusal_reason is not None
    assert "missing active/default" in preview.refusal_reason


def test_preview_refuses_duplicate_active_default_address() -> None:
    preview = planner.build_payout_plan_preview(
        credit_run_id=1,
        wallet_name="wallet",
        reserve_fraction=Decimal("0.50"),
        trusted_balance_snapshot=Decimal("300"),
        credit_run=_credit_run(),
        credits=[_credit()],
        address_lookup={
            "sc-2": [
                _address(),
                _address(address="az1other"),
            ]
        },
    )
    assert preview.plan_allowed is False
    assert preview.refusal_reason is not None
    assert "duplicate active/default" in preview.refusal_reason


def test_duplicate_draft_plan_for_same_credit_run_refuses() -> None:
    preview = planner.build_payout_plan_preview(
        credit_run_id=1,
        wallet_name="wallet",
        reserve_fraction=Decimal("0.50"),
        trusted_balance_snapshot=Decimal("300"),
        credit_run=_credit_run(),
        credits=[_credit()],
        address_lookup={"sc-2": [_address()]},
        existing_draft_plan_id=99,
    )
    assert preview.plan_allowed is False
    assert preview.refusal_reason is not None
    assert "draft payout plan already exists" in preview.refusal_reason


def test_insert_sql_touches_only_plan_tables() -> None:
    for builder in (
        planner.build_insert_payout_plan_sql,
        planner.build_insert_payout_plan_row_sql,
    ):
        sql = builder()
        tables = set(re.findall(r"insert\s+into\s+([a-z0-9_]+)", sql.lower()))
        assert tables <= {"sc_node_payout_plans", "sc_node_payout_plan_rows"}


def test_readonly_sql_is_select_only() -> None:
    for sql in (
        planner.build_credit_run_for_plan_sql(),
        planner.build_credits_for_plan_sql(),
        planner.build_active_default_payout_addresses_sql(),
        planner.build_existing_draft_plan_sql(),
        planner.build_payout_plans_sql(),
        planner.build_payout_plan_details_sql(1),
        planner.build_payout_plan_rows_sql(1),
    ):
        assert _MUTATING_SQL.search(sql) is None


def test_write_draft_insert_sql_present() -> None:
    plan_sql = planner.build_insert_payout_plan_sql()
    row_sql = planner.build_insert_payout_plan_row_sql()
    assert "RETURNING id" in plan_sql
    assert "sc_node_payout_plan_rows" in row_sql


def test_implementation_files_have_no_wallet_send_keywords() -> None:
    guard_block = re.compile(
        r"_WALLET_SEND_KEYWORDS = re\.compile\([\s\S]*?\)\n",
        re.MULTILINE,
    )
    for rel in (
        "payouts/collector/app/sc_node_payout_planner.py",
        "payouts/scripts/sc_node_payout_planner.py",
    ):
        text = (AZPOOL_ROOT / rel).read_text(encoding="utf-8")
        scrubbed = guard_block.sub("", text, count=1)
        assert _WALLET_SEND_KEYWORDS.search(scrubbed) is None


def test_script_has_no_subprocess_or_shell_true() -> None:
    source = (AZPOOL_ROOT / "payouts/scripts/sc_node_payout_planner.py").read_text(
        encoding="utf-8"
    )
    assert "subprocess" not in source
    assert "shell=True" not in source
