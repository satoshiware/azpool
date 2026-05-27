from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

from payouts.collector.app import admin_readonly
from payouts.collector.app import sc_node_manual_periodic_payout_runner as periodic_runner
from payouts.collector.app import sc_node_payout_cycle_readiness as cycle_readiness
from payouts.collector.app import sc_node_payout_production_preflight as production_preflight


_NOW = datetime(2026, 5, 27, 14, 0, 0, tzinfo=timezone.utc)
_LAST = _NOW - timedelta(minutes=30)


def _preflight(*, allowed: bool = True) -> dict[str, object]:
    return {
        "id": 2,
        "payout_plan_id": 2,
        "preflight_status": production_preflight.PREFLIGHT_STATUS_PASSED,
        "execution_allowed": allowed,
        "source_wallet_name": "wallet",
    }


def _last_confirmed(execution_id: int = 3) -> dict[str, object]:
    return {
        "id": execution_id,
        "payout_plan_id": 2,
        "status": "confirmed",
        "updated_at": _LAST,
        "created_at": _LAST,
    }


def test_default_cycle_interval_is_twenty_minutes() -> None:
    assert periodic_runner.parse_cycle_interval_minutes(cli_value=None, env_value="") == 20


def test_cycle_interval_rejects_non_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        periodic_runner.normalize_cycle_interval_minutes(0)


def test_cadence_eligible_after_interval_elapsed() -> None:
    cadence = periodic_runner.evaluate_cadence_eligibility(
        now=_NOW,
        cycle_interval_minutes=20,
        last_confirmed_execution=_last_confirmed(),
    )
    assert cadence.payout_cadence_policy == "periodic"
    assert cadence.immediate_payout_allowed is False
    assert cadence.cadence_eligible is True
    assert cadence.last_closed_execution_id == 3
    assert cadence.next_eligible_at is not None


def test_cadence_not_eligible_before_interval() -> None:
    cadence = periodic_runner.evaluate_cadence_eligibility(
        now=_LAST + timedelta(minutes=10),
        cycle_interval_minutes=20,
        last_confirmed_execution=_last_confirmed(),
    )
    assert cadence.cadence_eligible is False
    assert cadence.cadence_refusal_reason is not None


def test_cadence_override_requires_reason() -> None:
    cadence = periodic_runner.evaluate_cadence_eligibility(
        now=_LAST + timedelta(minutes=5),
        cycle_interval_minutes=20,
        last_confirmed_execution=_last_confirmed(),
        override_cadence_check=True,
        override_cadence_reason=None,
    )
    assert cadence.cadence_eligible is False
    assert "override-cadence-reason" in (cadence.cadence_refusal_reason or "")


def test_idempotency_blocks_duplicate_active_execution() -> None:
    assessment = periodic_runner.evaluate_idempotency_state(
        payout_plan_id=2,
        idempotency_key="production-real-v0-plan-2",
        plan_executions=[
            {
                "id": 4,
                "idempotency_key": "other-key",
                "status": "sent",
            }
        ],
    )
    assert assessment.may_execute is False
    assert assessment.blocking_execution_id == 4


def test_idempotency_replay_sent_execution() -> None:
    assessment = periodic_runner.evaluate_idempotency_state(
        payout_plan_id=2,
        idempotency_key="production-real-v0-plan-2",
        plan_executions=[
            {
                "id": 5,
                "idempotency_key": "production-real-v0-plan-2",
                "status": "confirmed",
            }
        ],
    )
    assert assessment.may_execute is False
    assert "idempotent replay" in (assessment.refusal_reason or "")


def test_idempotency_refuses_automatic_retry_after_refused() -> None:
    assessment = periodic_runner.evaluate_idempotency_state(
        payout_plan_id=2,
        idempotency_key="production-real-v0-plan-2",
        plan_executions=[
            {
                "id": 2,
                "idempotency_key": "production-real-v0-plan-2",
                "status": "refused",
            }
        ],
    )
    assert assessment.may_execute is False
    assert "automatic retry is forbidden" in (assessment.refusal_reason or "")


def test_runner_gates_refuse_halt_recommended_mode() -> None:
    cadence = periodic_runner.evaluate_cadence_eligibility(
        now=_NOW,
        cycle_interval_minutes=20,
        last_confirmed_execution=None,
    )
    idempotency = periodic_runner.evaluate_idempotency_state(
        payout_plan_id=2,
        idempotency_key="production-real-v0-plan-2",
        plan_executions=[],
    )
    gates = periodic_runner.evaluate_runner_gates(
        cadence=cadence,
        idempotency=idempotency,
        preflight=_preflight(),
        recommended_execution_mode="halt",
        runner_approval_phrase=periodic_runner.RUNNER_APPROVAL_PHRASE,
        require_runner_approval=True,
    )
    assert gates.allowed is False
    assert "halt" in (gates.refusal_reason or "")


def test_runner_gates_require_exact_approval_phrase() -> None:
    cadence = periodic_runner.evaluate_cadence_eligibility(
        now=_NOW,
        cycle_interval_minutes=20,
        last_confirmed_execution=None,
    )
    idempotency = periodic_runner.evaluate_idempotency_state(
        payout_plan_id=2,
        idempotency_key="production-real-v0-plan-2",
        plan_executions=[],
    )
    gates = periodic_runner.evaluate_runner_gates(
        cadence=cadence,
        idempotency=idempotency,
        preflight=_preflight(),
        recommended_execution_mode="single",
        runner_approval_phrase="NOPE",
        require_runner_approval=True,
    )
    assert gates.allowed is False
    assert "runner approval phrase" in (gates.refusal_reason or "")


def test_runner_gates_allow_single_when_all_pass() -> None:
    cadence = periodic_runner.evaluate_cadence_eligibility(
        now=_NOW,
        cycle_interval_minutes=20,
        last_confirmed_execution=None,
    )
    idempotency = periodic_runner.evaluate_idempotency_state(
        payout_plan_id=2,
        idempotency_key="production-real-v0-plan-2",
        plan_executions=[],
    )
    gates = periodic_runner.evaluate_runner_gates(
        cadence=cadence,
        idempotency=idempotency,
        preflight=_preflight(),
        recommended_execution_mode="single",
        runner_approval_phrase=periodic_runner.RUNNER_APPROVAL_PHRASE,
        require_runner_approval=True,
        readiness_verdict=cycle_readiness.VERDICT_CLOSED,
    )
    assert gates.allowed is True
    assert gates.runner_approval_verified is True


def test_delegate_argv_for_single_uses_executor_script() -> None:
    argv = periodic_runner.build_single_executor_delegate_argv(
        python_executable=sys.executable,
        repo_script_path=str(
            AZPOOL_ROOT / periodic_runner.single_executor_script_relpath()
        ),
        payout_plan_id=2,
        production_preflight_id=2,
        source_wallet_name="wallet",
        azc_bin="/usr/local/bin/azc-payout",
        idempotency_key="production-real-v0-plan-2",
        executor_confirm_phrase="SEND 223.125000000000 FROM wallet FOR PLAN 2",
    )
    assert argv[2] == "execute-real"
    assert "sc_node_payout_production_executor.py" in argv[1]
    assert argv[-2:] == [
        "--confirm-phrase",
        "SEND 223.125000000000 FROM wallet FOR PLAN 2",
    ]


def test_delegate_argv_for_chunked_includes_chunk_amount() -> None:
    argv = periodic_runner.build_chunked_executor_delegate_argv(
        python_executable=sys.executable,
        repo_script_path=str(
            AZPOOL_ROOT / periodic_runner.chunked_executor_script_relpath()
        ),
        payout_plan_id=2,
        production_preflight_id=2,
        source_wallet_name="wallet",
        azc_bin="/usr/local/bin/azc-payout",
        idempotency_key="production-chunked-v0-plan-2",
        executor_confirm_phrase=(
            "SEND CHUNKED 223.125000000000 FROM wallet FOR PLAN 2 IN 9 CHUNKS"
        ),
        chunk_amount="25",
    )
    assert "sc_node_payout_production_chunked_executor.py" in argv[1]
    assert "--chunk-amount" in argv
    chunk_idx = argv.index("--chunk-amount")
    assert argv[chunk_idx + 1].startswith("25")


def test_runner_sql_is_read_only() -> None:
    for sql in (
        periodic_runner.build_last_confirmed_execution_sql(),
        periodic_runner.build_plan_production_executions_sql(),
    ):
        admin_readonly.assert_readonly_sql(sql)


def test_runner_module_has_no_wallet_send_keywords() -> None:
    guard_block = re.compile(
        r"_FORBIDDEN_RUNNER_WALLET_KEYWORDS = re\.compile\([\s\S]*?\)\n",
        re.MULTILINE,
    )
    text = (
        AZPOOL_ROOT / "payouts/collector/app/sc_node_manual_periodic_payout_runner.py"
    ).read_text(encoding="utf-8")
    scrubbed = guard_block.sub("", text, count=1)
    assert re.search(r"\bsendtoaddress\b", scrubbed, re.IGNORECASE) is None
    assert re.search(r"\bsendmany\b", scrubbed, re.IGNORECASE) is None


def test_runner_script_delegates_without_sendtoaddress_literal() -> None:
    source = (
        AZPOOL_ROOT / "payouts/scripts/sc_node_manual_periodic_payout_runner.py"
    ).read_text(encoding="utf-8")
    assert "sendtoaddress" not in source
    assert "subprocess.run" in source
    assert "build_single_executor_delegate_argv" in source


def test_runner_help_exits_zero() -> None:
    from payouts.scripts import sc_node_manual_periodic_payout_runner as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0


def test_runner_preview_help_exits_zero() -> None:
    from payouts.scripts import sc_node_manual_periodic_payout_runner as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["preview", "--help"])
    assert exc.value.code == 0


def test_runner_execute_approved_help_exits_zero() -> None:
    from payouts.scripts import sc_node_manual_periodic_payout_runner as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["execute-approved", "--help"])
    assert exc.value.code == 0


def test_execute_approved_requires_idempotency_key() -> None:
    from payouts.scripts import sc_node_manual_periodic_payout_runner as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "execute-approved",
                "--payout-plan-id",
                "2",
                "--production-preflight-id",
                "2",
                "--recommended-execution-mode",
                "single",
                "--source-wallet-name",
                "wallet",
                "--runner-approval-phrase",
                periodic_runner.RUNNER_APPROVAL_PHRASE,
                "--executor-confirm-phrase",
                "SEND 1 FROM wallet FOR PLAN 2",
            ]
        )
    assert exc.value.code != 0
