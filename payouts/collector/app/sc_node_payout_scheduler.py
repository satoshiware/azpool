from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from payouts.collector.app import sc_node_manual_periodic_payout_runner as periodic_runner
from payouts.collector.app import sc_node_payout_cycle_readiness as cycle_readiness
from payouts.collector.app import sc_node_payout_production_executor as production_executor
from payouts.collector.app import sc_node_payout_production_preflight as production_preflight
from payouts.collector.app.sc_node_payout_planner import _serialize_datetime

MODE_REPORT_ONLY = "report-only"
MODE_DRY_RUN_DELEGATE = "dry-run-delegate"
MODE_EXECUTE_ENABLED = "execute-enabled"

SCHEDULER_MODES = frozenset({MODE_REPORT_ONLY, MODE_DRY_RUN_DELEGATE, MODE_EXECUTE_ENABLED})

ENABLE_REAL_EXECUTION_TOKEN = "YES_ENABLE_UNATTENDED_PAYOUT_EXECUTION"

ENV_SCHEDULER_MODE = "SC_NODE_PAYOUT_SCHEDULER_MODE"
ENV_PAYOUT_PLAN_ID = "SC_NODE_PAYOUT_SCHEDULER_PAYOUT_PLAN_ID"
ENV_PRODUCTION_PREFLIGHT_ID = "SC_NODE_PAYOUT_SCHEDULER_PRODUCTION_PREFLIGHT_ID"
ENV_RECOMMENDED_EXECUTION_MODE = "SC_NODE_PAYOUT_SCHEDULER_RECOMMENDED_EXECUTION_MODE"
ENV_ON_CALENDAR = "SC_NODE_PAYOUT_SCHEDULER_ON_CALENDAR"
ENV_RUNNER_APPROVAL_PHRASE = "SC_NODE_PAYOUT_SCHEDULER_RUNNER_APPROVAL_PHRASE"
ENV_EXECUTOR_CONFIRM_PHRASE = "SC_NODE_PAYOUT_SCHEDULER_EXECUTOR_CONFIRM_PHRASE"
ENV_IDEMPOTENCY_KEY = "SC_NODE_PAYOUT_SCHEDULER_IDEMPOTENCY_KEY"
ENV_SOURCE_WALLET_NAME = "SC_NODE_PAYOUT_SCHEDULER_SOURCE_WALLET_NAME"
ENV_AZC_BIN = "SC_NODE_PAYOUT_SCHEDULER_AZC_BIN"
ENV_CHUNK_AMOUNT = "SC_NODE_PAYOUT_SCHEDULER_CHUNK_AMOUNT"

SAFE_SKIP_PREFIX = "SAFE_SKIP"
RECOMMENDED_EXECUTION_MODES = frozenset({"single", "chunked", "halt"})

EXIT_SUCCESS = 0
EXIT_USAGE_ERROR = 1
EXIT_SAFE_SKIP = 2
EXIT_HALT = 3

_MANUAL_RUNNER_SCRIPT = "payouts/scripts/sc_node_manual_periodic_payout_runner.py"

_FORBIDDEN_SCHEDULER_WALLET_KEYWORDS = re.compile(
    r"\b("
    r"sendmany|sendtoaddress|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|importprivkey|importmulti|settxfee|bumpfee|"
    r"privkey|dumpwallet|azcoin-cli"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SchedulerTargetConfig:
    payout_plan_id: int | None
    production_preflight_id: int | None
    recommended_execution_mode: str | None
    explicit_target_configured: bool
    config_error: str | None


@dataclass(frozen=True)
class SchedulerExecutionConfig:
    runner_approval_phrase: str | None
    executor_confirm_phrase: str | None
    source_wallet_name: str | None
    azc_bin: str | None
    idempotency_key: str | None
    chunk_amount: str | None
    enable_real_execution: bool
    config_refusal_reason: str | None


@dataclass(frozen=True)
class SchedulerReport:
    timestamp: str
    scheduler_mode: str
    payout_plan_id: int
    production_preflight_id: int
    recommended_execution_mode: str
    would_execute: bool
    executed: bool
    delegated_command: list[str] | None
    refusal_reason: str | None
    gates: dict[str, Any]
    cadence: dict[str, Any]
    idempotency: dict[str, Any] | None
    readiness_verdict: str | None


def assert_no_forbidden_scheduler_wallet_keywords(text: str) -> None:
    if _FORBIDDEN_SCHEDULER_WALLET_KEYWORDS.search(text):
        raise ValueError("scheduler text must not contain wallet send or signing keywords")


def normalize_scheduler_mode(value: str) -> str:
    mode = str(value).strip()
    if mode not in SCHEDULER_MODES:
        raise ValueError(
            f"scheduler_mode must be one of: {', '.join(sorted(SCHEDULER_MODES))}"
        )
    return mode


def verify_enable_real_execution_flag(value: str | None) -> bool:
    return str(value or "").strip() == ENABLE_REAL_EXECUTION_TOKEN


def _parse_optional_positive_int(value: str | int | None) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except ValueError:
        return None
    if parsed <= 0:
        return None
    return parsed


def _parse_optional_recommended_execution_mode(value: str | None) -> str | None:
    if value is None:
        return None
    mode = str(value).strip().lower()
    if not mode:
        return None
    if mode not in RECOMMENDED_EXECUTION_MODES:
        return None
    return mode


def resolve_scheduler_target(
    *,
    payout_plan_id: int | None = None,
    production_preflight_id: int | None = None,
    recommended_execution_mode: str | None = None,
) -> SchedulerTargetConfig:
    plan_raw = (
        payout_plan_id
        if payout_plan_id is not None
        else os.environ.get(ENV_PAYOUT_PLAN_ID, "").strip() or None
    )
    preflight_raw = (
        production_preflight_id
        if production_preflight_id is not None
        else os.environ.get(ENV_PRODUCTION_PREFLIGHT_ID, "").strip() or None
    )
    mode_raw = (
        recommended_execution_mode
        if recommended_execution_mode is not None
        else os.environ.get(ENV_RECOMMENDED_EXECUTION_MODE, "").strip() or None
    )

    config_error: str | None = None
    plan_id = _parse_optional_positive_int(plan_raw)
    preflight_id = _parse_optional_positive_int(preflight_raw)
    mode = _parse_optional_recommended_execution_mode(mode_raw)

    if plan_raw is not None and str(plan_raw).strip() and plan_id is None:
        config_error = f"invalid {ENV_PAYOUT_PLAN_ID}: must be a positive integer"
    elif preflight_raw is not None and str(preflight_raw).strip() and preflight_id is None:
        config_error = (
            f"invalid {ENV_PRODUCTION_PREFLIGHT_ID}: must be a positive integer"
        )
    elif mode_raw is not None and str(mode_raw).strip() and mode is None:
        config_error = (
            f"invalid {ENV_RECOMMENDED_EXECUTION_MODE}: "
            f"must be one of {', '.join(sorted(RECOMMENDED_EXECUTION_MODES))}"
        )

    explicit = plan_id is not None and preflight_id is not None and mode is not None
    if not explicit and config_error is None:
        missing: list[str] = []
        if plan_id is None:
            missing.append(ENV_PAYOUT_PLAN_ID)
        if preflight_id is None:
            missing.append(ENV_PRODUCTION_PREFLIGHT_ID)
        if mode is None:
            missing.append(ENV_RECOMMENDED_EXECUTION_MODE)
        if missing:
            config_error = None

    return SchedulerTargetConfig(
        payout_plan_id=plan_id,
        production_preflight_id=preflight_id,
        recommended_execution_mode=mode,
        explicit_target_configured=explicit,
        config_error=config_error,
    )


def format_safe_skip_message(reason: str) -> str:
    return f"{SAFE_SKIP_PREFIX}: {reason}"


def validate_on_calendar_schedule(value: str | None) -> tuple[bool, str | None]:
    schedule = str(value or "").strip()
    if not schedule:
        return False, "OnCalendar schedule is empty"
    if schedule.startswith("@") and schedule.endswith("@"):
        return False, f"OnCalendar schedule is an unresolved placeholder: {schedule}"
    return True, None


def resolve_scheduler_mode(
    *,
    cli_value: str | None = None,
) -> str:
    raw = cli_value if cli_value is not None else os.environ.get(ENV_SCHEDULER_MODE, "").strip()
    if not raw:
        return MODE_REPORT_ONLY
    return normalize_scheduler_mode(raw)


def load_execution_config(
    *,
    enable_real_execution_flag: str | None,
    runner_approval_phrase: str | None = None,
    executor_confirm_phrase: str | None = None,
    source_wallet_name: str | None = None,
    azc_bin: str | None = None,
    idempotency_key: str | None = None,
    chunk_amount: str | None = None,
) -> SchedulerExecutionConfig:
    enabled = verify_enable_real_execution_flag(enable_real_execution_flag)
    runner_phrase = (
        runner_approval_phrase
        if runner_approval_phrase is not None
        else os.environ.get(ENV_RUNNER_APPROVAL_PHRASE, "").strip() or None
    )
    executor_phrase = (
        executor_confirm_phrase
        if executor_confirm_phrase is not None
        else os.environ.get(ENV_EXECUTOR_CONFIRM_PHRASE, "").strip() or None
    )
    wallet = (
        source_wallet_name
        if source_wallet_name is not None
        else os.environ.get(ENV_SOURCE_WALLET_NAME, "").strip() or None
    )
    azc = (
        azc_bin if azc_bin is not None else os.environ.get(ENV_AZC_BIN, "").strip() or None
    )
    idem = (
        idempotency_key
        if idempotency_key is not None
        else os.environ.get(ENV_IDEMPOTENCY_KEY, "").strip() or None
    )
    chunk = (
        chunk_amount
        if chunk_amount is not None
        else os.environ.get(ENV_CHUNK_AMOUNT, "").strip() or None
    )

    refusal: str | None = None
    if enabled:
        missing: list[str] = []
        if not runner_phrase:
            missing.append(ENV_RUNNER_APPROVAL_PHRASE)
        if not executor_phrase:
            missing.append(ENV_EXECUTOR_CONFIRM_PHRASE)
        if not wallet:
            missing.append(ENV_SOURCE_WALLET_NAME)
        if not idem:
            missing.append(ENV_IDEMPOTENCY_KEY)
        if missing:
            refusal = (
                "execute-enabled requires configured approval phrases and ids: "
                + ", ".join(missing)
            )

    return SchedulerExecutionConfig(
        runner_approval_phrase=runner_phrase,
        executor_confirm_phrase=executor_phrase,
        source_wallet_name=wallet,
        azc_bin=azc or "/usr/local/bin/azc-payout",
        idempotency_key=idem,
        chunk_amount=chunk,
        enable_real_execution=enabled,
        config_refusal_reason=refusal,
    )


def build_manual_runner_delegate_argv(
    *,
    python_executable: str,
    repo_root: str,
    payout_plan_id: int,
    production_preflight_id: int,
    recommended_execution_mode: str,
    cycle_interval_minutes: int,
    idempotency_key: str,
    source_wallet_name: str,
    azc_bin: str,
    runner_approval_phrase: str,
    executor_confirm_phrase: str,
    readiness_production_execution_id: int | None = None,
    chunk_amount: str | None = None,
    dry_run_delegate: bool = False,
) -> list[str]:
    assert_no_forbidden_scheduler_wallet_keywords(python_executable)
    script_path = f"{repo_root.rstrip('/')}/{_MANUAL_RUNNER_SCRIPT}"
    assert_no_forbidden_scheduler_wallet_keywords(script_path)
    assert_no_forbidden_scheduler_wallet_keywords(source_wallet_name)
    assert_no_forbidden_scheduler_wallet_keywords(idempotency_key)
    assert_no_forbidden_scheduler_wallet_keywords(runner_approval_phrase)
    assert_no_forbidden_scheduler_wallet_keywords(executor_confirm_phrase)
    assert_no_forbidden_scheduler_wallet_keywords(azc_bin)

    argv = [
        python_executable,
        script_path,
        "execute-approved",
        "--payout-plan-id",
        str(int(payout_plan_id)),
        "--production-preflight-id",
        str(int(production_preflight_id)),
        "--recommended-execution-mode",
        periodic_runner.normalize_recommended_execution_mode(recommended_execution_mode),
        "--cycle-interval-minutes",
        str(int(cycle_interval_minutes)),
        "--source-wallet-name",
        production_executor.normalize_source_wallet_name(source_wallet_name),
        "--azc-bin",
        azc_bin,
        "--idempotency-key",
        production_executor.normalize_idempotency_key(idempotency_key),
        "--runner-approval-phrase",
        periodic_runner.normalize_runner_approval_phrase(runner_approval_phrase),
        "--executor-confirm-phrase",
        executor_confirm_phrase.strip(),
    ]
    if readiness_production_execution_id is not None:
        argv.extend(
            [
                "--readiness-production-execution-id",
                str(int(readiness_production_execution_id)),
            ]
        )
    if chunk_amount is not None:
        argv.extend(["--chunk-amount", str(chunk_amount)])
    if dry_run_delegate:
        argv.append("--dry-run-delegate")
    for arg in argv:
        assert_no_forbidden_scheduler_wallet_keywords(arg)
    return argv


def build_scheduler_report(
    *,
    scheduler_mode: str,
    payout_plan_id: int,
    production_preflight_id: int,
    recommended_execution_mode: str,
    gate_payload: Mapping[str, Any],
    execution_config: SchedulerExecutionConfig | None = None,
    delegated_command: list[str] | None = None,
    executed: bool = False,
    refusal_reason: str | None = None,
    now: datetime | None = None,
) -> SchedulerReport:
    mode = normalize_scheduler_mode(scheduler_mode)
    gates = dict(gate_payload.get("gates", {}))
    cadence = dict(gates.get("cadence", {}))
    idempotency_raw = gates.get("idempotency")
    idempotency = dict(idempotency_raw) if isinstance(idempotency_raw, Mapping) else None
    readiness_verdict = gates.get("readiness_verdict")
    if isinstance(readiness_verdict, str):
        readiness_verdict_value: str | None = readiness_verdict
    else:
        readiness_verdict_value = None

    allowed = bool(gates.get("allowed"))
    would_execute = allowed and mode != MODE_REPORT_ONLY
    if mode == MODE_EXECUTE_ENABLED:
        would_execute = (
            allowed
            and execution_config is not None
            and execution_config.enable_real_execution
            and execution_config.config_refusal_reason is None
        )

    combined_refusal = refusal_reason or gates.get("refusal_reason")
    if mode == MODE_EXECUTE_ENABLED and execution_config is not None:
        if execution_config.config_refusal_reason:
            combined_refusal = (
                f"{combined_refusal}; {execution_config.config_refusal_reason}"
                if combined_refusal
                else execution_config.config_refusal_reason
            )
        elif not execution_config.enable_real_execution:
            combined_refusal = (
                f"{combined_refusal}; execute-enabled requires "
                f"--enable-real-execution {ENABLE_REAL_EXECUTION_TOKEN}"
                if combined_refusal
                else f"execute-enabled requires --enable-real-execution "
                f"{ENABLE_REAL_EXECUTION_TOKEN}"
            )

    timestamp = _serialize_datetime(
        now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)
    )
    return SchedulerReport(
        timestamp=timestamp,
        scheduler_mode=mode,
        payout_plan_id=int(payout_plan_id),
        production_preflight_id=int(production_preflight_id),
        recommended_execution_mode=periodic_runner.normalize_recommended_execution_mode(
            recommended_execution_mode
        ),
        would_execute=would_execute,
        executed=executed,
        delegated_command=delegated_command,
        refusal_reason=combined_refusal,
        gates=gates,
        cadence=cadence,
        idempotency=idempotency,
        readiness_verdict=readiness_verdict_value,
    )


def scheduler_report_to_dict(report: SchedulerReport) -> dict[str, Any]:
    return {
        "timestamp": report.timestamp,
        "scheduler_mode": report.scheduler_mode,
        "payout_plan_id": report.payout_plan_id,
        "production_preflight_id": report.production_preflight_id,
        "recommended_execution_mode": report.recommended_execution_mode,
        "would_execute": report.would_execute,
        "executed": report.executed,
        "delegated_command": report.delegated_command,
        "refusal_reason": report.refusal_reason,
        "cadence": report.cadence,
        "idempotency": report.idempotency,
        "readiness_verdict": report.readiness_verdict,
        "gates": report.gates,
        "accounting_note": (
            "scheduler v0 wraps PR Y manual runner; default report-only; "
            "no new wallet send primitives"
        ),
    }


def format_scheduler_text(report: SchedulerReport) -> str:
    lines = [
        f"Scheduler mode: {report.scheduler_mode}",
        f"Timestamp: {report.timestamp}",
        f"Payout plan: {report.payout_plan_id}",
        f"Production preflight: {report.production_preflight_id}",
        f"Recommended execution mode: {report.recommended_execution_mode}",
        f"Cadence eligible: {report.cadence.get('cadence_eligible')}",
        f"Cycle interval minutes: {report.cadence.get('cycle_interval_minutes')}",
        f"Would execute: {report.would_execute}",
        f"Executed: {report.executed}",
    ]
    if report.readiness_verdict is not None:
        lines.append(f"Readiness verdict: {report.readiness_verdict}")
    if report.idempotency is not None:
        lines.append(f"Idempotency may_execute: {report.idempotency.get('may_execute')}")
    if report.refusal_reason:
        lines.append(f"Refusal: {report.refusal_reason}")
    if report.delegated_command:
        lines.append(f"Delegated command: {' '.join(report.delegated_command)}")
    return "\n".join(lines) + "\n"


def scheduler_exit_code(report: SchedulerReport) -> int:
    verdict = report.readiness_verdict
    if verdict == cycle_readiness.VERDICT_HALT:
        return EXIT_HALT
    if verdict == cycle_readiness.VERDICT_NEEDS_EVIDENCE:
        return EXIT_HALT
    if report.refusal_reason and "requires configured approval" in report.refusal_reason:
        return EXIT_USAGE_ERROR
    if report.refusal_reason and report.refusal_reason.startswith(SAFE_SKIP_PREFIX):
        return EXIT_SUCCESS
    if report.refusal_reason and "requires --enable-real-execution" in report.refusal_reason:
        return EXIT_SUCCESS
    if not report.cadence.get("cadence_eligible", False):
        return EXIT_SAFE_SKIP
    if not bool(report.gates.get("allowed")):
        return EXIT_SAFE_SKIP
    if report.scheduler_mode == MODE_EXECUTE_ENABLED and not report.executed:
        if report.refusal_reason:
            return EXIT_SAFE_SKIP
    return EXIT_SUCCESS


def manual_runner_script_relpath() -> str:
    return _MANUAL_RUNNER_SCRIPT
