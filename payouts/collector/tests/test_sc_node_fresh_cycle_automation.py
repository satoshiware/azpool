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

from payouts.collector.app import sc_node_credit_ledger as credit_ledger
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
        min_payout_amount=None,
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
        min_payout_amount=None,
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
        min_payout_amount=None,
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
        min_payout_amount=None,
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


def test_resolve_azc_bin_honors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        fresh.ENV_AZC_BIN,
        "/usr/local/bin/azc-payout-readonly",
    )
    assert fresh.resolve_azc_bin() == "/usr/local/bin/azc-payout-readonly"


def test_resolve_azc_bin_defaults_to_readonly_wrapper() -> None:
    assert fresh.resolve_azc_bin() == fresh.DEFAULT_AZC_BIN_READONLY


def test_preview_tiny_payout_with_balance_recommends_single_not_unexplained_halt() -> None:
    preview = production_preflight.ProductionPayoutPreflightPreview(
        payout_plan_id=0,
        source_wallet_name="wallet",
        execution_allowed=True,
        refusal_reason=None,
        wallet_balance=production_preflight.WalletBalance(
            trusted=Decimal("1000"),
            immature=Decimal("0"),
        ),
        planned_amount_total=Decimal("1.875000000000"),
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
            spendable_balance=Decimal("1000"),
            planned_payout_amount=Decimal("1.875000000000"),
            reserve_requirement=Decimal("500"),
            available_after_reserve=Decimal("500"),
            utxo_count=21,
            max_observed_utxo_amount=Decimal("2"),
            target_single_tx_max_amount=Decimal("500"),
            fallback_chunk_amount=Decimal("25"),
            recommended_chunk_size=Decimal("1.875000000000"),
            estimated_chunk_count=1,
            fragmentation_risk=production_preflight.FRAGMENTATION_RISK_LOW,
            recommended_execution_mode=production_preflight.RECOMMENDED_EXECUTION_MODE_SINGLE,
            refusal_reason=None,
            wallet_utxo_source=production_preflight.WALLET_UTXO_SOURCE_AZC_LISTUNSPENT,
            utxo_evidence_note=None,
        ),
    )
    plan = fresh.build_execution_plan(
        preflight_preview=preview,
        payout_plan_id=0,
        source_wallet_name="wallet",
    )
    assert plan.recommended_execution_mode == production_preflight.RECOMMENDED_EXECUTION_MODE_SINGLE
    assert plan.refusal_reason is None


def test_halt_always_includes_refusal_reason() -> None:
    preview = production_preflight.ProductionPayoutPreflightPreview(
        payout_plan_id=0,
        source_wallet_name="wallet",
        execution_allowed=False,
        refusal_reason="planned_amount_total exceeds spendable_after_reserve (1)",
        wallet_balance=production_preflight.WalletBalance(
            trusted=Decimal("1"),
            immature=Decimal("0"),
        ),
        planned_amount_total=Decimal("1.875000000000"),
        reserve_mode=production_preflight.RESERVE_MODE_PERCENT,
        reserve_percent=Decimal("0.5"),
        reserve_amount=Decimal("0.5"),
        spendable_after_reserve=Decimal("0.5"),
        max_spend_percent=Decimal("0.5"),
        max_spend_allowed=Decimal("0.5"),
        operator_override=False,
        row_count=1,
        rows=(),
        utxo_chunking_policy=production_preflight.UtxoChunkingPolicy(
            spendable_balance=Decimal("1"),
            planned_payout_amount=Decimal("1.875000000000"),
            reserve_requirement=Decimal("0.5"),
            available_after_reserve=Decimal("0.5"),
            utxo_count=1,
            max_observed_utxo_amount=Decimal("1"),
            target_single_tx_max_amount=Decimal("500"),
            fallback_chunk_amount=Decimal("25"),
            recommended_chunk_size=Decimal("25"),
            estimated_chunk_count=1,
            fragmentation_risk=production_preflight.FRAGMENTATION_RISK_LOW,
            recommended_execution_mode=production_preflight.RECOMMENDED_EXECUTION_MODE_HALT,
            refusal_reason="planned_amount_total exceeds spendable_after_reserve (1)",
            wallet_utxo_source=production_preflight.WALLET_UTXO_SOURCE_AZC_LISTUNSPENT,
            utxo_evidence_note=None,
        ),
    )
    plan = fresh.build_execution_plan(
        preflight_preview=preview,
        payout_plan_id=0,
        source_wallet_name="wallet",
    )
    assert plan.recommended_execution_mode == production_preflight.RECOMMENDED_EXECUTION_MODE_HALT
    assert plan.refusal_reason is not None
    assert "spendable_after_reserve" in plan.refusal_reason


def test_preview_summary_includes_preflight_fields_when_halted() -> None:
    preview = production_preflight.ProductionPayoutPreflightPreview(
        payout_plan_id=0,
        source_wallet_name="wallet",
        execution_allowed=False,
        refusal_reason="insufficient balance",
        wallet_balance=production_preflight.WalletBalance(
            trusted=Decimal("1"),
            immature=Decimal("0"),
        ),
        planned_amount_total=Decimal("1.875000000000"),
        reserve_mode=production_preflight.RESERVE_MODE_PERCENT,
        reserve_percent=Decimal("0.5"),
        reserve_amount=Decimal("0.5"),
        spendable_after_reserve=Decimal("0.5"),
        max_spend_percent=Decimal("0.5"),
        max_spend_allowed=Decimal("0.5"),
        operator_override=False,
        row_count=1,
        rows=(),
        utxo_chunking_policy=production_preflight.UtxoChunkingPolicy(
            spendable_balance=Decimal("1"),
            planned_payout_amount=Decimal("1.875000000000"),
            reserve_requirement=Decimal("0.5"),
            available_after_reserve=Decimal("0.5"),
            utxo_count=1,
            max_observed_utxo_amount=Decimal("1"),
            target_single_tx_max_amount=Decimal("500"),
            fallback_chunk_amount=Decimal("25"),
            recommended_chunk_size=Decimal("25"),
            estimated_chunk_count=1,
            fragmentation_risk=production_preflight.FRAGMENTATION_RISK_LOW,
            recommended_execution_mode=production_preflight.RECOMMENDED_EXECUTION_MODE_HALT,
            refusal_reason="insufficient balance",
            wallet_utxo_source=production_preflight.WALLET_UTXO_SOURCE_AZC_LISTUNSPENT,
            utxo_evidence_note=None,
        ),
    )
    execution_plan = fresh.build_execution_plan(
        preflight_preview=preview,
        payout_plan_id=0,
        source_wallet_name="wallet",
    )
    config = fresh.load_config_from_env(mode_override=fresh.MODE_PREVIEW)
    payload = fresh.build_preview_summary(
        config=config,
        selection=fresh.build_fresh_cycle_selection(
            config=config,
            unlinked_events=[_event(2, "1.875000000000", _PRIOR_END + timedelta(hours=1))],
            latest_credit_run_coverage_end=_PRIOR_END,
            exclude_coverage_start_boundary=False,
        ),
        credit_preview=None,
        preflight_preview=preview,
        execution_plan=execution_plan,
    )
    assert payload["recommended_execution_mode"] == "halt"
    assert payload["refusal_reason"] is not None
    assert payload["execution_allowed"] is False
    assert payload["preflight_status"] == production_preflight.PREFLIGHT_STATUS_REFUSED
    assert "utxo_chunking_policy" in payload


def test_scan_rewards_uses_configured_azc_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    from payouts.scripts import sc_node_fresh_cycle_automation as cli

    captured: dict[str, str] = {}

    def _fake_scan(*, wallet_name: str, azc_bin: str) -> None:
        captured["wallet_name"] = wallet_name
        captured["azc_bin"] = azc_bin

    monkeypatch.setattr(cli, "_maybe_scan_rewards", _fake_scan)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv(fresh.ENV_BASELINE, "2026-05-28T14:50:30+00:00")
    monkeypatch.setenv(fresh.ENV_AZC_BIN, "/usr/local/bin/azc-payout-readonly")

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def set_read_only(self, _value: bool) -> None:
            return None

    monkeypatch.setattr(cli.psycopg, "connect", lambda _url: _FakeConn())
    monkeypatch.setattr(cli, "_load_selection", lambda *args, **kwargs: None)
    cli.main(["preview", "--scan-rewards-first", "--json"])
    assert captured["azc_bin"] == "/usr/local/bin/azc-payout-readonly"


def test_payout_plan_row_insert_params_match_planner_schema() -> None:
    from payouts.collector.app import sc_node_payout_planner as planner

    row = planner.PayoutPlanRowPreview(
        credit_id=1,
        sc_node_id="node-1",
        sc_node_display_name="SC Node 1",
        payout_address="azc1addr",
        gross_credit_amount=Decimal("1.875000000000"),
        correction_amount=Decimal("0"),
        payout_amount=Decimal("1.875000000000"),
    )
    params = fresh.build_payout_plan_row_insert_params(payout_plan_id=5, row=row)
    required = fresh.required_payout_plan_row_insert_param_names()
    assert required == set(params.keys())
    assert params["row_status"] == "draft"
    assert params["sc_node_display_name"] == "SC Node 1"
    assert "status" not in params


def test_install_script_sets_pool_ledger_azledger_permissions() -> None:
    text = (
        AZPOOL_ROOT / "deploy/scripts/install-azcoin-sc-node-fresh-cycle-automation.sh"
    ).read_text(encoding="utf-8")
    assert "g azledger" in text
    assert "0660" in text
    assert 'POOL_LEDGER_DIR="/etc/azcoin-super/pool-ledger"' in text


def test_service_unit_uses_environmentfile_not_shell_source() -> None:
    text = (
        AZPOOL_ROOT / "deploy/systemd/azcoin-sc-node-fresh-cycle-automation.service"
    ).read_text(encoding="utf-8")
    assert "source /etc/azcoin-super/pool-ledger/fresh-cycle-automation.env" not in text
    assert "EnvironmentFile=-/etc/azcoin-super/pool-ledger/fresh-cycle-automation.env" in text


def test_write_scheduler_env_file_writes_atomically_with_group_mode(tmp_path: Path) -> None:
    target = tmp_path / "payout-scheduler.env"
    fresh.write_scheduler_env_file(
        str(target),
        fresh.build_safe_skip_scheduler_env_lines(),
    )
    content = target.read_text(encoding="utf-8")
    assert "SC_NODE_PAYOUT_SCHEDULER_MODE=report-only" in content
    assert (target.stat().st_mode & 0o777) == fresh.DEFAULT_SCHEDULER_ENV_FILE_MODE


def test_partial_artifact_refusal_explains_resume() -> None:
    config = fresh.FreshCycleConfig(
        automation_baseline=_BASELINE,
        mode=fresh.MODE_WRITE_TARGET,
        wallet_name="wallet",
        reserve_fraction=Decimal("0.50"),
        target_single_tx_max_amount=Decimal("500"),
        fallback_chunk_amount=Decimal("25"),
        enable_real_execution=False,
        runner_approval_phrase=None,
        azc_bin="azc",
        approved_by="test",
        scheduler_env_path="/tmp/payout-scheduler.env",
        min_payout_amount=None,
    )
    selection = fresh.build_fresh_cycle_selection(
        config=config,
        unlinked_events=[_event(2, "1.875000000000", _PRIOR_END + timedelta(hours=1))],
        latest_credit_run_coverage_end=_PRIOR_END,
        exclude_coverage_start_boundary=False,
    )
    assert selection is not None
    lookup = fresh.FreshCycleArtifactLookup(6, None, None)
    msg = fresh.evaluate_partial_artifact_refusal(lookup=lookup, selection=selection)
    assert msg is not None
    assert "credit_run_id=6" in msg
    assert "without payout plan" in msg


def test_write_artifacts_resumes_partial_credit_run_without_duplicate_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from payouts.scripts import sc_node_fresh_cycle_automation as cli

    config = fresh.FreshCycleConfig(
        automation_baseline=_BASELINE,
        mode=fresh.MODE_WRITE_TARGET,
        wallet_name="wallet",
        reserve_fraction=Decimal("0.50"),
        target_single_tx_max_amount=Decimal("500"),
        fallback_chunk_amount=Decimal("25"),
        enable_real_execution=False,
        runner_approval_phrase=None,
        azc_bin="azc",
        approved_by="test",
        scheduler_env_path="/tmp/payout-scheduler.env",
        min_payout_amount=None,
    )
    selection = fresh.build_fresh_cycle_selection(
        config=config,
        unlinked_events=[_event(2, "1.875000000000", _PRIOR_END + timedelta(hours=1))],
        latest_credit_run_coverage_end=_PRIOR_END,
        exclude_coverage_start_boundary=False,
    )
    assert selection is not None

    write_plan_calls = {"count": 0}

    monkeypatch.setattr(
        cli,
        "_lookup_fresh_cycle_artifacts",
        lambda *args, **kwargs: fresh.FreshCycleArtifactLookup(
            6,
            None,
            None,
            resume_note="resume plan",
        ),
    )
    monkeypatch.setattr(
        cli,
        "_load_credit_preview",
        lambda *args, **kwargs: credit_ledger.CreditRunPreview(
            wallet_name="wallet",
            coverage=fresh.build_credit_coverage(selection),
            reward_event_count=1,
            reward_amount_total=Decimal("1.875000000000"),
            mapped_work_total=Decimal("1"),
            sc_node_credits=(),
            unmapped_work=credit_ledger.UnmappedWorkPreview(
                work_delta_total=Decimal("0"),
                accepted_delta_total=Decimal("0"),
                delta_rows=0,
            ),
            allocation_allowed=True,
            refusal_reason=None,
        ),
    )
    monkeypatch.setattr(cli, "_write_credit_run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("duplicate credit run")))
    monkeypatch.setattr(
        cli.preflight_cli,
        "_run_getbalances",
        lambda **kwargs: {"mine": {"trusted": "1000", "immature": "0"}},
    )
    monkeypatch.setattr(cli.preflight_cli, "_run_listunspent", lambda **kwargs: [])
    def _fake_write_plan(*args: object, **kwargs: object) -> int:
        write_plan_calls["count"] += 1
        return 7

    monkeypatch.setattr(cli, "_write_payout_plan", _fake_write_plan)
    monkeypatch.setattr(cli, "_approve_plan", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli,
        "_record_preflight",
        lambda *args, **kwargs: (
            8,
            production_preflight.ProductionPayoutPreflightPreview(
                payout_plan_id=7,
                source_wallet_name="wallet",
                execution_allowed=True,
                refusal_reason=None,
                wallet_balance=production_preflight.WalletBalance(
                    trusted=Decimal("1000"),
                    immature=Decimal("0"),
                ),
                planned_amount_total=Decimal("1.875000000000"),
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
                    spendable_balance=Decimal("1000"),
                    planned_payout_amount=Decimal("1.875000000000"),
                    reserve_requirement=Decimal("500"),
                    available_after_reserve=Decimal("500"),
                    utxo_count=1,
                    max_observed_utxo_amount=Decimal("2"),
                    target_single_tx_max_amount=Decimal("500"),
                    fallback_chunk_amount=Decimal("25"),
                    recommended_chunk_size=Decimal("1.875000000000"),
                    estimated_chunk_count=1,
                    fragmentation_risk=production_preflight.FRAGMENTATION_RISK_LOW,
                    recommended_execution_mode=production_preflight.RECOMMENDED_EXECUTION_MODE_SINGLE,
                    refusal_reason=None,
                    wallet_utxo_source=production_preflight.WALLET_UTXO_SOURCE_AZC_LISTUNSPENT,
                    utxo_evidence_note=None,
                ),
            ),
        ),
    )

    credit_run_id, payout_plan_id, preflight_id, _, resume_note = cli._write_artifacts(
        MagicMock(),
        config=config,
        selection=selection,
    )
    assert credit_run_id == 6
    assert payout_plan_id == 7
    assert preflight_id == 8
    assert resume_note == "resume plan"
    assert write_plan_calls["count"] == 1


def test_write_target_writes_scheduler_env_after_db_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from payouts.scripts import sc_node_fresh_cycle_automation as cli

    scheduler_path = tmp_path / "payout-scheduler.env"
    config = fresh.FreshCycleConfig(
        automation_baseline=_BASELINE,
        mode=fresh.MODE_WRITE_TARGET,
        wallet_name="wallet",
        reserve_fraction=Decimal("0.50"),
        target_single_tx_max_amount=Decimal("500"),
        fallback_chunk_amount=Decimal("25"),
        enable_real_execution=False,
        runner_approval_phrase=None,
        azc_bin="azc",
        approved_by="test",
        scheduler_env_path=str(scheduler_path),
        min_payout_amount=None,
    )
    selection = fresh.build_fresh_cycle_selection(
        config=config,
        unlinked_events=[_event(2, "1.875000000000", _PRIOR_END + timedelta(hours=1))],
        latest_credit_run_coverage_end=_PRIOR_END,
        exclude_coverage_start_boundary=False,
    )
    assert selection is not None
    preview = production_preflight.ProductionPayoutPreflightPreview(
        payout_plan_id=7,
        source_wallet_name="wallet",
        execution_allowed=True,
        refusal_reason=None,
        wallet_balance=production_preflight.WalletBalance(
            trusted=Decimal("1000"),
            immature=Decimal("0"),
        ),
        planned_amount_total=Decimal("1.875000000000"),
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
            spendable_balance=Decimal("1000"),
            planned_payout_amount=Decimal("1.875000000000"),
            reserve_requirement=Decimal("500"),
            available_after_reserve=Decimal("500"),
            utxo_count=1,
            max_observed_utxo_amount=Decimal("2"),
            target_single_tx_max_amount=Decimal("500"),
            fallback_chunk_amount=Decimal("25"),
            recommended_chunk_size=Decimal("1.875000000000"),
            estimated_chunk_count=1,
            fragmentation_risk=production_preflight.FRAGMENTATION_RISK_LOW,
            recommended_execution_mode=production_preflight.RECOMMENDED_EXECUTION_MODE_SINGLE,
            refusal_reason=None,
            wallet_utxo_source=production_preflight.WALLET_UTXO_SOURCE_AZC_LISTUNSPENT,
            utxo_evidence_note=None,
        ),
    )

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def commit(self) -> None:
            return None

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv(fresh.ENV_BASELINE, "2026-05-28T14:50:30+00:00")
    monkeypatch.setattr(cli.psycopg, "connect", lambda _url: _FakeConn())
    monkeypatch.setattr(cli, "_load_selection", lambda *args, **kwargs: selection)
    monkeypatch.setattr(
        cli,
        "_write_artifacts",
        lambda *args, **kwargs: (6, 7, 8, preview, None),
    )
    assert cli.main(["--scheduler-env-path", str(scheduler_path), "write-target", "--json"]) == 0
    text = scheduler_path.read_text(encoding="utf-8")
    assert "SC_NODE_PAYOUT_SCHEDULER_PAYOUT_PLAN_ID=7" in text
    assert "SC_NODE_PAYOUT_SCHEDULER_PRODUCTION_PREFLIGHT_ID=8" in text


def test_execute_live_finally_attempts_scheduler_restore_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from payouts.scripts import sc_node_fresh_cycle_automation as cli

    scheduler_path = tmp_path / "payout-scheduler.env"
    scheduler_path.write_text("placeholder\n", encoding="utf-8")
    restore_calls: list[str] = []

    def _fake_restore(path: str) -> None:
        restore_calls.append(path)
        fresh.write_scheduler_env_file(path, fresh.build_safe_skip_scheduler_env_lines())

    monkeypatch.setattr(cli, "_restore_safe_scheduler_env", _fake_restore)
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv(fresh.ENV_BASELINE, "2026-05-28T14:50:30+00:00")
    monkeypatch.setenv(fresh.ENV_ENABLE_REAL_EXECUTION, fresh.ENABLE_REAL_EXECUTION_TOKEN)
    monkeypatch.setenv(fresh.ENV_RUNNER_APPROVAL_PHRASE, fresh.RUNNER_APPROVAL_PHRASE)

    def _boom_connect(_url: str) -> None:
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(cli.psycopg, "connect", _boom_connect)
    rc = cli.main(
        [
            "--scheduler-env-path",
            str(scheduler_path),
            "execute-live",
        ]
    )
    assert rc != 0
    assert restore_calls == [str(scheduler_path)]
    assert "SC_NODE_PAYOUT_SCHEDULER_MODE=report-only" in scheduler_path.read_text(encoding="utf-8")


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
