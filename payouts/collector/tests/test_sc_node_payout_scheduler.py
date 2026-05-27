from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

from payouts.collector.app import sc_node_manual_periodic_payout_runner as periodic_runner
from payouts.collector.app import sc_node_payout_cycle_readiness as cycle_readiness
from payouts.collector.app import sc_node_payout_scheduler as scheduler


_NOW = datetime(2026, 5, 27, 14, 0, 0, tzinfo=timezone.utc)


def _gate_payload(*, allowed: bool = True, cadence_eligible: bool = True) -> dict[str, object]:
    cadence = periodic_runner.evaluate_cadence_eligibility(
        now=_NOW,
        cycle_interval_minutes=20,
        last_confirmed_execution=None,
    )
    if not cadence_eligible:
        cadence = periodic_runner.evaluate_cadence_eligibility(
            now=_NOW - timedelta(minutes=5),
            cycle_interval_minutes=20,
            last_confirmed_execution={
                "id": 3,
                "updated_at": _NOW - timedelta(minutes=10),
            },
        )
    idempotency = periodic_runner.evaluate_idempotency_state(
        payout_plan_id=2,
        idempotency_key="production-chunked-v0-plan-2",
        plan_executions=[],
    )
    gates = periodic_runner.evaluate_runner_gates(
        cadence=cadence,
        idempotency=idempotency,
        preflight={
            "preflight_status": "passed",
            "execution_allowed": True,
        },
        recommended_execution_mode="chunked",
    )
    if not allowed:
        gates = periodic_runner.evaluate_runner_gates(
            cadence=cadence,
            idempotency=idempotency,
            preflight={
                "preflight_status": "passed",
                "execution_allowed": True,
            },
            recommended_execution_mode="halt",
        )
    return {"gates": periodic_runner.runner_gate_result_to_dict(gates)}


def test_default_scheduler_mode_is_report_only() -> None:
    assert scheduler.normalize_scheduler_mode("report-only") == scheduler.MODE_REPORT_ONLY


def test_enable_real_execution_token_required() -> None:
    assert scheduler.verify_enable_real_execution_flag("YES_ENABLE_UNATTENDED_PAYOUT_EXECUTION")
    assert not scheduler.verify_enable_real_execution_flag("NO")


def test_execute_enabled_config_refuses_missing_phrases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(scheduler.ENV_RUNNER_APPROVAL_PHRASE, raising=False)
    monkeypatch.delenv(scheduler.ENV_EXECUTOR_CONFIRM_PHRASE, raising=False)
    config = scheduler.load_execution_config(
        enable_real_execution_flag=scheduler.ENABLE_REAL_EXECUTION_TOKEN,
    )
    assert config.enable_real_execution is True
    assert config.config_refusal_reason is not None
    assert scheduler.ENV_RUNNER_APPROVAL_PHRASE in config.config_refusal_reason


def test_build_manual_runner_delegate_argv_uses_runner_script() -> None:
    argv = scheduler.build_manual_runner_delegate_argv(
        python_executable=sys.executable,
        repo_root=str(AZPOOL_ROOT),
        payout_plan_id=2,
        production_preflight_id=2,
        recommended_execution_mode="chunked",
        cycle_interval_minutes=20,
        idempotency_key="production-chunked-v0-plan-2",
        source_wallet_name="wallet",
        azc_bin="/usr/local/bin/azc-payout",
        runner_approval_phrase=periodic_runner.RUNNER_APPROVAL_PHRASE,
        executor_confirm_phrase="SEND CHUNKED 223.125000000000 FROM wallet FOR PLAN 2 IN 9 CHUNKS",
        chunk_amount="25",
        dry_run_delegate=True,
    )
    assert "sc_node_manual_periodic_payout_runner.py" in argv[1]
    assert argv[2] == "execute-approved"
    assert "--dry-run-delegate" in argv
    assert "sendtoaddress" not in " ".join(argv)


def test_report_only_would_not_execute() -> None:
    report = scheduler.build_scheduler_report(
        scheduler_mode=scheduler.MODE_REPORT_ONLY,
        payout_plan_id=2,
        production_preflight_id=2,
        recommended_execution_mode="single",
        gate_payload=_gate_payload(),
        now=_NOW,
    )
    assert report.would_execute is False
    assert report.executed is False


def test_dry_run_mode_would_execute_when_gates_pass() -> None:
    report = scheduler.build_scheduler_report(
        scheduler_mode=scheduler.MODE_DRY_RUN_DELEGATE,
        payout_plan_id=2,
        production_preflight_id=2,
        recommended_execution_mode="single",
        gate_payload=_gate_payload(),
        delegated_command=["python", "runner.py", "execute-approved"],
        now=_NOW,
    )
    assert report.would_execute is True


def test_scheduler_exit_code_safe_skip_when_cadence_not_eligible() -> None:
    report = scheduler.build_scheduler_report(
        scheduler_mode=scheduler.MODE_REPORT_ONLY,
        payout_plan_id=2,
        production_preflight_id=2,
        recommended_execution_mode="single",
        gate_payload=_gate_payload(cadence_eligible=False),
        now=_NOW,
    )
    assert scheduler.scheduler_exit_code(report) == scheduler.EXIT_SAFE_SKIP


def test_scheduler_exit_code_halt_on_readiness_verdict() -> None:
    payload = _gate_payload()
    payload["gates"]["readiness_verdict"] = cycle_readiness.VERDICT_HALT
    report = scheduler.build_scheduler_report(
        scheduler_mode=scheduler.MODE_REPORT_ONLY,
        payout_plan_id=2,
        production_preflight_id=2,
        recommended_execution_mode="single",
        gate_payload=payload,
        now=_NOW,
    )
    assert scheduler.scheduler_exit_code(report) == scheduler.EXIT_HALT


def test_scheduler_module_has_no_sendtoaddress() -> None:
    guard_block = re.compile(
        r"_FORBIDDEN_SCHEDULER_WALLET_KEYWORDS = re\.compile\([\s\S]*?\)\n",
        re.MULTILINE,
    )
    text = (AZPOOL_ROOT / "payouts/collector/app/sc_node_payout_scheduler.py").read_text(
        encoding="utf-8"
    )
    scrubbed = guard_block.sub("", text, count=1)
    assert re.search(r"\bsendtoaddress\b", scrubbed, re.IGNORECASE) is None


def test_scheduler_script_has_no_sendtoaddress_literal() -> None:
    source = (AZPOOL_ROOT / "payouts/scripts/sc_node_payout_scheduler.py").read_text(
        encoding="utf-8"
    )
    assert "sendtoaddress" not in source
    assert "build_manual_runner_delegate_argv" in source or "delegated_command" in source


def test_scheduler_help_exits_zero() -> None:
    from payouts.scripts import sc_node_payout_scheduler as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
