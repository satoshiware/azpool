from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

from payouts.collector.app import sc_node_credit_ledger as ledger


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

_TS_POOL_START = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
_TS_POOL_END = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
_TS_REWARD_START = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
_TS_REWARD_END = datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc)


def test_build_sc_node_work_share_sql_uses_observed_interval_columns() -> None:
    sql = ledger.build_sc_node_work_share_sql()
    assert "observed_from" in sql
    assert "observed_to" in sql
    assert "observed_at" not in sql
    assert _MUTATING_SQL.search(sql) is None


def test_eligible_rewards_filter_mature_only() -> None:
    sql = ledger.build_eligible_mature_rewards_sql()
    assert "maturity_status = 'mature'" in sql
    assert "immature" not in sql
    assert "orphaned" not in sql


def test_eligible_rewards_sql_uses_half_open_coverage_interval() -> None:
    sql = ledger.build_eligible_mature_rewards_sql()
    assert "event_time < %(coverage_end)s" in sql
    assert "event_time <= %(coverage_end)s" not in sql
    assert "exclude_coverage_start_boundary" in sql


def test_prior_credit_run_coverage_end_match_sql_is_select_only() -> None:
    sql = ledger.build_prior_credit_run_coverage_end_match_sql()
    assert _MUTATING_SQL.search(sql) is None
    assert "exclude_coverage_start_boundary" in sql


def test_reward_event_at_coverage_start_included_for_first_cycle() -> None:
    coverage_start = datetime(2026, 5, 21, 21, 41, 42, 359511, tzinfo=timezone.utc)
    event_time = datetime(2026, 5, 21, 21, 41, 42, 359511, tzinfo=timezone.utc)
    coverage_end = datetime(2026, 5, 26, 15, 13, 32, tzinfo=timezone.utc)
    assert ledger.reward_event_time_in_coverage(
        event_time,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        exclude_coverage_start_boundary=False,
    )


def test_reward_event_at_coverage_end_is_excluded() -> None:
    coverage_start = datetime(2026, 5, 21, 21, 41, 42, 359511, tzinfo=timezone.utc)
    coverage_end = datetime(2026, 5, 26, 15, 13, 32, tzinfo=timezone.utc)
    event_time = coverage_end
    assert not ledger.reward_event_time_in_coverage(
        event_time,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        exclude_coverage_start_boundary=False,
    )


def test_adjacent_cycle_excludes_boundary_event_at_shared_timestamp() -> None:
    boundary = datetime(2026, 5, 26, 15, 13, 32, tzinfo=timezone.utc)
    cycle3_end = datetime(2026, 5, 27, 21, 58, 42, tzinfo=timezone.utc)
    assert not ledger.reward_event_time_in_coverage(
        boundary,
        coverage_start=boundary,
        coverage_end=cycle3_end,
        exclude_coverage_start_boundary=True,
    )


def test_cycle2_cycle3_boundary_regression_reward_event_2282() -> None:
    boundary = datetime(2026, 5, 26, 15, 13, 32, tzinfo=timezone.utc)
    cycle2_start = datetime(2026, 5, 21, 21, 41, 42, 359511, tzinfo=timezone.utc)
    cycle3_end = datetime(2026, 5, 27, 21, 58, 42, tzinfo=timezone.utc)

    assert ledger.reward_event_time_in_coverage(
        boundary,
        coverage_start=cycle2_start,
        coverage_end=boundary,
        exclude_coverage_start_boundary=False,
    ) is False
    assert ledger.reward_event_time_in_coverage(
        boundary,
        coverage_start=boundary,
        coverage_end=cycle3_end,
        exclude_coverage_start_boundary=True,
    ) is False


def test_unmapped_work_sql_filters_null_sc_node_id() -> None:
    sql = ledger.build_unmapped_work_sql()
    assert "sc_node_id IS NULL" in sql


def test_sc_node_work_share_requires_mapped_sc_node_id() -> None:
    sql = ledger.build_sc_node_work_share_sql()
    assert "sc_node_id IS NOT NULL" in sql


def test_insert_sql_touches_only_credit_tables() -> None:
    for builder in (
        ledger.build_insert_credit_run_sql,
        ledger.build_insert_credit_sql,
        ledger.build_insert_credit_run_event_sql,
    ):
        sql = builder()
        lowered = sql.lower()
        assert "insert into" in lowered
        tables = set(re.findall(r"insert\s+into\s+([a-z0-9_]+)", lowered))
        assert tables <= {
            "sc_node_reward_credit_runs",
            "sc_node_reward_credits",
            "sc_node_reward_credit_run_events",
        }


def test_read_only_admin_sql_is_select_only() -> None:
    for sql in (
        ledger.build_credit_runs_sql(),
        ledger.build_credit_run_details_sql(1),
        ledger.build_credit_run_credits_sql(1),
        ledger.build_credit_run_events_sql(1),
        ledger.build_existing_reward_event_links_sql(),
        ledger.build_payout_plans_for_credit_run_sql(),
        ledger.build_production_executions_for_credit_run_sql(),
        ledger.build_prior_credit_run_coverage_end_match_sql(),
    ):
        assert _MUTATING_SQL.search(sql) is None


def test_credit_run_preview_dict_includes_required_totals() -> None:
    coverage = ledger.CreditCoverage(
        coverage_start=_TS_POOL_START,
        coverage_end=_TS_POOL_END,
        pool_coverage_start=_TS_POOL_START,
        pool_coverage_end=_TS_POOL_END,
        reward_coverage_start=_TS_REWARD_START,
        reward_coverage_end=_TS_REWARD_END,
        coverage_gap=False,
        operator_selected=True,
    )
    preview = ledger.build_credit_run_preview(
        wallet_name="SUPPORT",
        coverage=coverage,
        reward_rows=[{"amount": Decimal("10")}],
        sc_node_rows=[
            {"sc_node_id": "sc-2", "sc_node_display_name": "SC 2", "work_delta_total": 3},
            {"sc_node_id": "sc-3", "sc_node_display_name": "SC 3", "work_delta_total": 1},
        ],
        unmapped_row={"work_delta_total": 2, "accepted_delta_total": 1, "delta_rows": 1},
    )
    payload = ledger.credit_run_preview_to_dict(preview)
    assert payload["coverage_start"] == coverage.coverage_start.isoformat()
    assert payload["coverage_end"] == coverage.coverage_end.isoformat()
    assert payload["reward_event_count"] == 1
    assert payload["reward_amount_total"] == "10"
    assert payload["mapped_work_total"] == "4"
    assert payload["unmapped_work_total"] == "2"
    assert "user_identity" not in payload
    assert all("user_identity" not in row for row in payload["sc_node_credits"])


def test_mapped_work_total_zero_refuses_allocation() -> None:
    coverage = ledger.CreditCoverage(
        coverage_start=_TS_POOL_START,
        coverage_end=_TS_POOL_END,
        pool_coverage_start=_TS_POOL_START,
        pool_coverage_end=_TS_POOL_END,
        reward_coverage_start=_TS_REWARD_START,
        reward_coverage_end=_TS_REWARD_END,
        coverage_gap=False,
        operator_selected=True,
    )
    preview = ledger.build_credit_run_preview(
        wallet_name="SUPPORT",
        coverage=coverage,
        reward_rows=[{"amount": Decimal("5")}],
        sc_node_rows=[],
        unmapped_row=None,
    )
    assert preview.allocation_allowed is False
    assert preview.refusal_reason is not None
    assert "mapped_work_total" in preview.refusal_reason


def test_no_eligible_rewards_refuses_allocation() -> None:
    coverage = ledger.CreditCoverage(
        coverage_start=_TS_POOL_START,
        coverage_end=_TS_POOL_END,
        pool_coverage_start=_TS_POOL_START,
        pool_coverage_end=_TS_POOL_END,
        reward_coverage_start=_TS_REWARD_START,
        reward_coverage_end=_TS_REWARD_END,
        coverage_gap=False,
        operator_selected=True,
    )
    preview = ledger.build_credit_run_preview(
        wallet_name="SUPPORT",
        coverage=coverage,
        reward_rows=[],
        sc_node_rows=[
            {"sc_node_id": "sc-2", "sc_node_display_name": None, "work_delta_total": 1}
        ],
        unmapped_row=None,
    )
    assert preview.allocation_allowed is False
    assert preview.refusal_reason is not None
    assert "eligible mature rewards" in preview.refusal_reason


def test_write_draft_requires_explicit_coverage_or_allow_default() -> None:
    coverage = ledger.CreditCoverage(
        coverage_start=_TS_POOL_START,
        coverage_end=_TS_POOL_END,
        pool_coverage_start=_TS_POOL_START,
        pool_coverage_end=_TS_POOL_END,
        reward_coverage_start=_TS_REWARD_START,
        reward_coverage_end=_TS_REWARD_END,
        coverage_gap=False,
        operator_selected=False,
    )
    refusal = ledger.evaluate_write_draft_coverage_refusal(
        coverage=coverage,
        explicit_coverage=False,
        allow_default_coverage=False,
    )
    assert refusal is not None
    assert "coverage-start" in refusal


def test_write_draft_allows_explicit_coverage() -> None:
    coverage = ledger.CreditCoverage(
        coverage_start=_TS_POOL_START,
        coverage_end=_TS_POOL_END,
        pool_coverage_start=_TS_POOL_START,
        pool_coverage_end=_TS_POOL_END,
        reward_coverage_start=_TS_REWARD_START,
        reward_coverage_end=_TS_REWARD_END,
        coverage_gap=False,
        operator_selected=True,
    )
    refusal = ledger.evaluate_write_draft_coverage_refusal(
        coverage=coverage,
        explicit_coverage=True,
        allow_default_coverage=False,
    )
    assert refusal is None


def test_implementation_files_do_not_introduce_wallet_send_keywords() -> None:
    guard_block = re.compile(
        r"_WALLET_SEND_KEYWORDS = re\.compile\([\s\S]*?\)\n",
        re.MULTILINE,
    )
    for rel in (
        "payouts/collector/app/sc_node_credit_ledger.py",
        "payouts/scripts/sc_node_credit_ledger.py",
    ):
        text = (AZPOOL_ROOT / rel).read_text(encoding="utf-8")
        scrubbed = guard_block.sub("", text, count=1)
        assert _WALLET_SEND_KEYWORDS.search(scrubbed) is None


def test_script_has_no_wallet_rpc_or_shell_true() -> None:
    source = (AZPOOL_ROOT / "payouts/scripts/sc_node_credit_ledger.py").read_text(
        encoding="utf-8"
    )
    assert "subprocess" not in source
    assert "shell=True" not in source
    assert "listtransactions" not in source


def test_write_draft_duplicate_refuses_unpaid_existing_draft() -> None:
    refusal = ledger.evaluate_write_draft_duplicate_refusal(
        existing_links=[{"reward_event_id": 2282, "credit_run_id": 3}],
        payout_plans=[],
        production_executions=[],
    )
    assert refusal == (
        "reward event already linked to credit_run_id=3; "
        "existing unpaid duplicate draft — manual cleanup required before re-draft"
    )


def test_write_draft_duplicate_refuses_existing_payout_plan() -> None:
    refusal = ledger.evaluate_write_draft_duplicate_refusal(
        existing_links=[{"reward_event_id": 2282, "credit_run_id": 3}],
        payout_plans=[{"id": 2, "credit_run_id": 3, "status": "draft"}],
        production_executions=[],
    )
    assert refusal is not None
    assert "reward event already linked to credit_run_id=3" in refusal
    assert "payout plan(s) [2]" in refusal


def test_write_draft_duplicate_refuses_existing_production_execution() -> None:
    refusal = ledger.evaluate_write_draft_duplicate_refusal(
        existing_links=[{"reward_event_id": 2282, "credit_run_id": 4}],
        payout_plans=[{"id": 3, "credit_run_id": 4, "status": "approved"}],
        production_executions=[{"id": 5, "status": "sent", "credit_run_id": 4}],
    )
    assert refusal is not None
    assert "reward event already linked to credit_run_id=4" in refusal
    assert "production execution(s) [5]" in refusal


def test_preview_mode_has_no_write_draft_duplicate_checks() -> None:
    source = (AZPOOL_ROOT / "payouts/scripts/sc_node_credit_ledger.py").read_text(
        encoding="utf-8"
    )
    preview_block = source.split("def _cmd_preview")[1].split("def _cmd_write_draft")[0]
    assert "build_existing_reward_event_links_sql" not in preview_block
    assert "build_insert_credit_run_sql" not in preview_block
