from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

from payouts.collector.app import sc_node_fresh_cycle_automation as fresh
from payouts.collector.app import sc_node_payout_production_preflight as production_preflight
from payouts.collector.app import sc_node_payout_scheduler as scheduler


_BASELINE = datetime(2026, 5, 28, 14, 50, 30, tzinfo=timezone.utc)
_PRIOR_END = datetime(2026, 5, 28, 16, 0, 0, tzinfo=timezone.utc)


def _event(event_id: int, amount: str, event_time: datetime) -> dict[str, object]:
    return {
        "reward_event_id": event_id,
        "txid": f"tx-{event_id}",
        "amount": Decimal(amount),
        "event_time": event_time,
        "maturity_status": "mature",
    }


def test_zero_fresh_rewards_returns_none_selection() -> None:
    config = fresh.load_config_from_env(mode_override=fresh.MODE_PREVIEW)
    config = fresh.FreshCycleConfig(
        automation_baseline=_BASELINE,
        mode=fresh.MODE_PREVIEW,
        wallet_name="wallet",
        reserve_fraction=Decimal("0.50"),
        target_single_tx_max_amount=Decimal("500"),
        fallback_chunk_amount=Decimal("25"),
        enable_real_execution=False,
        runner_approval_phrase=None,
        azc_bin="azc",
        approved_by="test",
        scheduler_env_path="/tmp/payout-scheduler.env",
    )
    historical = _event(1, "10", _BASELINE - timedelta(hours=1))
    fresh_after = _event(2, "5", _PRIOR_END + timedelta(hours=1))
    selection = fresh.build_fresh_cycle_selection(
        config=config,
        unlinked_events=[historical],
        latest_credit_run_coverage_end=_PRIOR_END,
        exclude_coverage_start_boundary=False,
    )
    assert selection is None

    selection2 = fresh.build_fresh_cycle_selection(
        config=config,
        unlinked_events=[historical, fresh_after],
        latest_credit_run_coverage_end=_PRIOR_END,
        exclude_coverage_start_boundary=False,
    )
    assert selection2 is not None
    assert selection2.event_count == 1


def test_rewards_before_baseline_counted_as_historical_backlog_only() -> None:
    config = fresh.FreshCycleConfig(
        automation_baseline=_BASELINE,
        mode=fresh.MODE_PREVIEW,
        wallet_name="wallet",
        reserve_fraction=Decimal("0.50"),
        target_single_tx_max_amount=Decimal("500"),
        fallback_chunk_amount=Decimal("25"),
        enable_real_execution=False,
        runner_approval_phrase=None,
        azc_bin="azc",
        approved_by="test",
        scheduler_env_path="/tmp/payout-scheduler.env",
    )
    events = [
        _event(1, "100", _BASELINE - timedelta(days=1)),
        _event(2, "5", _BASELINE + timedelta(hours=2)),
    ]
    selection = fresh.build_fresh_cycle_selection(
        config=config,
        unlinked_events=events,
        latest_credit_run_coverage_end=None,
        exclude_coverage_start_boundary=False,
    )
    assert selection is not None
    assert selection.historical_backlog_count == 1
    assert selection.historical_backlog_amount == Decimal("100")
    assert selection.event_count == 1


def test_latest_credit_run_coverage_end_used_as_boundary() -> None:
    start = fresh.compute_coverage_start(
        automation_baseline=_BASELINE,
        latest_credit_run_coverage_end=_PRIOR_END,
    )
    assert start == _PRIOR_END


def test_no_default_coverage_path_in_module() -> None:
    source = (AZPOOL_ROOT / "payouts/collector/app/sc_node_fresh_cycle_automation.py").read_text(
        encoding="utf-8"
    )
    assert "resolve_default_coverage" not in source
    assert "allow_default_coverage" not in source


def test_malformed_baseline_refused() -> None:
    with pytest.raises(ValueError):
        fresh.parse_automation_baseline("not-a-timestamp")


def test_preview_summary_safe_skip() -> None:
    config = fresh.load_config_from_env(mode_override=fresh.MODE_PREVIEW)
    payload = fresh.build_preview_summary(
        config=config,
        selection=None,
        credit_preview=None,
    )
    assert payload["safe_skip"] is True
    assert payload["would_write"] is False
    assert payload["would_execute"] is False


def test_scheduler_env_report_only_target_written_correctly() -> None:
    lines = fresh.build_scheduler_target_env_lines(
        payout_plan_id=7,
        production_preflight_id=8,
        recommended_execution_mode="chunked",
        source_wallet_name="wallet",
        chunk_amount=Decimal("1.875000000000"),
    )
    text = fresh.render_scheduler_env_content(lines)
    assert "SC_NODE_PAYOUT_SCHEDULER_MODE=report-only" in text
    assert "SC_NODE_PAYOUT_SCHEDULER_PAYOUT_PLAN_ID=7" in text
    assert "SC_NODE_PAYOUT_SCHEDULER_PRODUCTION_PREFLIGHT_ID=8" in text
    assert "SC_NODE_PAYOUT_SCHEDULER_RECOMMENDED_EXECUTION_MODE=chunked" in text
    assert "SC_NODE_PAYOUT_SCHEDULER_CHUNK_AMOUNT=1.875000000000" in text


def test_execute_live_refuses_without_enable_token() -> None:
    config = fresh.FreshCycleConfig(
        automation_baseline=_BASELINE,
        mode=fresh.MODE_EXECUTE_LIVE,
        wallet_name="wallet",
        reserve_fraction=Decimal("0.50"),
        target_single_tx_max_amount=Decimal("500"),
        fallback_chunk_amount=Decimal("25"),
        enable_real_execution=False,
        runner_approval_phrase=fresh.RUNNER_APPROVAL_PHRASE,
        azc_bin="azc",
        approved_by="test",
        scheduler_env_path="/tmp/payout-scheduler.env",
    )
    refusal = fresh.evaluate_execute_live_refusal(config)
    assert refusal is not None
    assert fresh.ENV_ENABLE_REAL_EXECUTION in refusal


def test_execute_live_refuses_without_runner_phrase() -> None:
    config = fresh.FreshCycleConfig(
        automation_baseline=_BASELINE,
        mode=fresh.MODE_EXECUTE_LIVE,
        wallet_name="wallet",
        reserve_fraction=Decimal("0.50"),
        target_single_tx_max_amount=Decimal("500"),
        fallback_chunk_amount=Decimal("25"),
        enable_real_execution=True,
        runner_approval_phrase="NOPE",
        azc_bin="azc",
        approved_by="test",
        scheduler_env_path="/tmp/payout-scheduler.env",
    )
    refusal = fresh.evaluate_execute_live_refusal(config)
    assert refusal is not None
    assert fresh.ENV_RUNNER_APPROVAL_PHRASE in refusal


def test_execute_live_builds_expected_chunked_confirmation_phrase() -> None:
    preview = production_preflight.ProductionPayoutPreflightPreview(
        payout_plan_id=5,
        source_wallet_name="wallet",
        execution_allowed=True,
        refusal_reason=None,
        wallet_balance=production_preflight.WalletBalance(
            trusted=Decimal("1000"),
            immature=Decimal("0"),
        ),
        planned_amount_total=Decimal("39.375000000000"),
        reserve_mode=production_preflight.RESERVE_MODE_PERCENT,
        reserve_percent=Decimal("0.5"),
        reserve_amount=Decimal("500"),
        spendable_after_reserve=Decimal("500"),
        max_spend_percent=Decimal("0.5"),
        max_spend_allowed=Decimal("500"),
        operator_override=False,
        row_count=1,
        rows=(),
        utxo_chunking_policy=production_preflight.UtxoChunkingPolicy(
            spendable_balance=Decimal("500"),
            planned_payout_amount=Decimal("39.375000000000"),
            reserve_requirement=Decimal("500"),
            available_after_reserve=Decimal("500"),
            utxo_count=21,
            max_observed_utxo_amount=Decimal("2"),
            target_single_tx_max_amount=Decimal("500"),
            fallback_chunk_amount=Decimal("25"),
            recommended_chunk_size=Decimal("1.875000000000"),
            estimated_chunk_count=21,
            fragmentation_risk=production_preflight.FRAGMENTATION_RISK_LOW,
            recommended_execution_mode=production_preflight.RECOMMENDED_EXECUTION_MODE_CHUNKED,
            refusal_reason=None,
            wallet_utxo_source=production_preflight.WALLET_UTXO_SOURCE_AZC_LISTUNSPENT,
            utxo_evidence_note=None,
        ),
    )
    plan = fresh.build_execution_plan(
        preflight_preview=preview,
        payout_plan_id=5,
        source_wallet_name="wallet",
    )
    assert plan.executor_confirm_phrase == (
        "SEND CHUNKED 39.375000000000 FROM wallet FOR PLAN 5 IN 21 CHUNKS"
    )


def test_execute_live_delegates_to_manual_runner_not_direct_send() -> None:
    argv = fresh.build_manual_runner_execute_argv(
        python_executable=sys.executable,
        repo_root=str(AZPOOL_ROOT),
        payout_plan_id=5,
        production_preflight_id=5,
        recommended_execution_mode="chunked",
        idempotency_key="FRESH-CYCLE-6-PLAN-5-PREFLIGHT-5-EXECUTE-V1",
        source_wallet_name="wallet",
        azc_bin="/usr/local/bin/azc-payout",
        runner_approval_phrase=fresh.RUNNER_APPROVAL_PHRASE,
        executor_confirm_phrase="SEND CHUNKED 39.375000000000 FROM wallet FOR PLAN 5 IN 21 CHUNKS",
        chunk_amount=Decimal("1.875000000000"),
    )
    assert "sc_node_manual_periodic_payout_runner.py" in argv[1]
    assert argv[2] == "execute-approved"
    assert "--override-cadence-check" in argv
    assert "sendtoaddress" not in " ".join(argv)


def test_scheduler_delegate_requires_unattended_execution_flag() -> None:
    with pytest.raises(ValueError):
        fresh.build_scheduler_delegate_argv(
            python_executable=sys.executable,
            repo_root=str(AZPOOL_ROOT),
            payout_plan_id=5,
            production_preflight_id=5,
            recommended_execution_mode="chunked",
            idempotency_key="key",
            source_wallet_name="wallet",
            azc_bin="azc",
            runner_approval_phrase=fresh.RUNNER_APPROVAL_PHRASE,
            executor_confirm_phrase="SEND CHUNKED 1 FROM wallet FOR PLAN 5 IN 1 CHUNKS",
            enable_real_execution=False,
        )


def test_idempotency_key_format() -> None:
    key = fresh.build_execution_idempotency_key(
        credit_run_id=6,
        payout_plan_id=5,
        production_preflight_id=5,
    )
    assert key == "FRESH-CYCLE-6-PLAN-5-PREFLIGHT-5-EXECUTE-V1"


def test_redact_secret_text_masks_phrases() -> None:
    raw = (
        "SC_NODE_PAYOUT_SCHEDULER_RUNNER_APPROVAL_PHRASE=YES_I_APPROVE\n"
        "--executor-confirm-phrase SEND CHUNKED 1 FROM wallet FOR PLAN 1 IN 1 CHUNKS\n"
    )
    redacted = fresh.redact_secret_text(raw)
    assert "YES_I_APPROVE" not in redacted
    assert "***REDACTED***" in redacted


def test_preview_command_safe_skips_without_db(monkeypatch: pytest.MonkeyPatch) -> None:
    from payouts.scripts import sc_node_fresh_cycle_automation as cli

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def set_read_only(self, _value: bool) -> None:
            return None

    monkeypatch.setattr(cli.psycopg, "connect", lambda _url: _FakeConn())
    monkeypatch.setattr(cli, "_load_selection", lambda *args, **kwargs: None)
    assert cli.main(["preview", "--json"]) == 0


def test_execute_live_cli_refuses_without_policy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from payouts.scripts import sc_node_fresh_cycle_automation as cli

    monkeypatch.delenv(fresh.ENV_ENABLE_REAL_EXECUTION, raising=False)
    assert cli.main(["execute-live"]) == 1


def test_timer_template_has_nonempty_oncalendar_placeholder() -> None:
    template = AZPOOL_ROOT / "deploy/systemd/azcoin-sc-node-fresh-cycle-automation.timer.template"
    text = template.read_text(encoding="utf-8")
    assert "OnCalendar=@AZCOIN_FRESH_CYCLE_AUTOMATION_ON_CALENDAR@" in text
    assert "OnCalendar=\n" not in text


def test_module_has_no_sendtoaddress_literal() -> None:
    text = (AZPOOL_ROOT / "payouts/collector/app/sc_node_fresh_cycle_automation.py").read_text(
        encoding="utf-8"
    )
    scrubbed = re.sub(
        r"_FORBIDDEN_AUTOMATION_WALLET_KEYWORDS = re\.compile\([\s\S]*?\)\n",
        "",
        text,
        count=1,
    )
    assert re.search(r"\bsendtoaddress\b", scrubbed, re.IGNORECASE) is None
