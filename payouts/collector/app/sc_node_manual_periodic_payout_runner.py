from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from payouts.collector.app import admin_readonly
from payouts.collector.app import sc_node_payout_cycle_readiness as cycle_readiness
from payouts.collector.app import sc_node_payout_production_chunked_executor as chunked_executor
from payouts.collector.app import sc_node_payout_production_executor as production_executor
from payouts.collector.app import sc_node_payout_production_preflight as production_preflight
from payouts.collector.app import sc_node_payout_status_summary as status_summary
from payouts.collector.app.sc_node_payout_planner import _serialize_datetime

DEFAULT_CYCLE_INTERVAL_MINUTES = 20
ENV_CYCLE_INTERVAL_MINUTES = "SC_NODE_PAYOUT_CYCLE_INTERVAL_MINUTES"

PAYOUT_CADENCE_POLICY = "periodic"
IMMEDIATE_PAYOUT_ALLOWED = False

RUNNER_APPROVAL_PHRASE = "YES_I_APPROVE_PERIODIC_SC_NODE_PAYOUT"

MODE_PREVIEW = "preview"
MODE_EXECUTE_APPROVED = "execute-approved"

EXECUTOR_MODE_SINGLE = production_preflight.RECOMMENDED_EXECUTION_MODE_SINGLE
EXECUTOR_MODE_CHUNKED = production_preflight.RECOMMENDED_EXECUTION_MODE_CHUNKED
EXECUTOR_MODE_HALT = production_preflight.RECOMMENDED_EXECUTION_MODE_HALT

_IDEMPOTENT_COMPLETE_STATUSES = frozenset(
    {
        production_executor.EXECUTION_STATUS_SENT,
        production_executor.EXECUTION_STATUS_CONFIRMED,
    }
)
_BLOCKING_PLAN_STATUSES = frozenset(
    {
        production_executor.EXECUTION_STATUS_SENT,
        production_executor.EXECUTION_STATUS_CONFIRMED,
        chunked_executor.EXECUTION_STATUS_PARTIAL_SENT,
    }
)
_AUTO_RETRY_FORBIDDEN_STATUSES = frozenset(
    {
        production_executor.EXECUTION_STATUS_REFUSED,
        production_executor.EXECUTION_STATUS_VOID,
        chunked_executor.EXECUTION_STATUS_PARTIAL_SENT,
    }
)

_FORBIDDEN_RUNNER_WALLET_KEYWORDS = re.compile(
    r"\b("
    r"sendmany|sendtoaddress|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|importprivkey|importmulti|settxfee|bumpfee|"
    r"privkey|dumpwallet"
    r")\b",
    re.IGNORECASE,
)

_SINGLE_EXECUTOR_SCRIPT = "payouts/scripts/sc_node_payout_production_executor.py"
_CHUNKED_EXECUTOR_SCRIPT = "payouts/scripts/sc_node_payout_production_chunked_executor.py"


@dataclass(frozen=True)
class CadenceEligibility:
    payout_cadence_policy: str
    immediate_payout_allowed: bool
    cycle_interval_minutes: int
    last_closed_execution_id: int | None
    last_closed_at: str | None
    last_confirmed_at: str | None
    next_eligible_at: str | None
    cadence_eligible: bool
    cadence_refusal_reason: str | None
    cadence_evidence_note: str | None


@dataclass(frozen=True)
class IdempotencyAssessment:
    idempotency_key: str
    existing_execution_id: int | None
    existing_execution_status: str | None
    plan_has_blocking_execution: bool
    blocking_execution_id: int | None
    blocking_execution_status: str | None
    may_execute: bool
    refusal_reason: str | None


@dataclass(frozen=True)
class RunnerGateResult:
    allowed: bool
    refusal_reason: str | None
    cadence: CadenceEligibility
    idempotency: IdempotencyAssessment
    readiness_verdict: str | None
    readiness_refusal_reason: str | None
    preflight_execution_allowed: bool | None
    recommended_execution_mode: str | None
    runner_approval_verified: bool


def assert_no_forbidden_runner_wallet_keywords(text: str) -> None:
    if _FORBIDDEN_RUNNER_WALLET_KEYWORDS.search(text):
        raise ValueError("runner text must not contain wallet send or signing keywords")


def normalize_cycle_interval_minutes(value: int | str) -> int:
    minutes = int(value)
    if minutes <= 0:
        raise ValueError("cycle_interval_minutes must be a positive integer")
    return minutes


def parse_cycle_interval_minutes(
    *,
    cli_value: int | str | None = None,
    env_value: str | None = None,
) -> int:
    if cli_value is not None:
        return normalize_cycle_interval_minutes(cli_value)
    raw = env_value if env_value is not None else os.environ.get(ENV_CYCLE_INTERVAL_MINUTES, "")
    if str(raw).strip():
        return normalize_cycle_interval_minutes(str(raw).strip())
    return DEFAULT_CYCLE_INTERVAL_MINUTES


def normalize_runner_approval_phrase(value: str) -> str:
    phrase = str(value).strip()
    if not phrase:
        raise ValueError("runner approval phrase is required")
    return phrase


def verify_runner_approval_phrase(phrase: str) -> bool:
    return normalize_runner_approval_phrase(phrase) == RUNNER_APPROVAL_PHRASE


def normalize_recommended_execution_mode(value: str) -> str:
    mode = str(value).strip().lower()
    if mode not in {EXECUTOR_MODE_SINGLE, EXECUTOR_MODE_CHUNKED, EXECUTOR_MODE_HALT}:
        raise ValueError(
            "recommended_execution_mode must be one of: single, chunked, halt"
        )
    return mode


def parse_optional_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_last_confirmed_execution_sql() -> str:
    sql = """
SELECT
  id,
  payout_plan_id,
  status,
  updated_at,
  created_at
FROM sc_node_payout_production_executions
WHERE status = 'confirmed'
ORDER BY updated_at DESC, id DESC
LIMIT 1
""".strip()
    admin_readonly.assert_readonly_sql(sql)
    return sql


def build_plan_production_executions_sql() -> str:
    sql = """
SELECT
  id,
  payout_plan_id,
  production_preflight_id,
  source_wallet_name,
  status,
  idempotency_key,
  txid,
  refusal_reason,
  notes,
  created_at,
  updated_at
FROM sc_node_payout_production_executions
WHERE payout_plan_id = %(payout_plan_id)s
ORDER BY id DESC
""".strip()
    admin_readonly.assert_readonly_sql(sql)
    return sql


def _serialize_optional_datetime(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _serialize_datetime(value)
    return _serialize_datetime(value)


def evaluate_cadence_eligibility(
    *,
    now: datetime,
    cycle_interval_minutes: int,
    last_confirmed_execution: Mapping[str, Any] | None,
    last_cycle_at_override: datetime | None = None,
    override_cadence_check: bool = False,
    override_cadence_reason: str | None = None,
) -> CadenceEligibility:
    interval = normalize_cycle_interval_minutes(cycle_interval_minutes)
    anchor: datetime | None = last_cycle_at_override
    last_closed_execution_id: int | None = None
    last_confirmed_at: str | None = None
    evidence_note: str | None = None

    if last_confirmed_execution is not None:
        last_closed_execution_id = int(last_confirmed_execution["id"])
        db_updated = last_confirmed_execution.get("updated_at")
        if isinstance(db_updated, datetime):
            if anchor is None:
                anchor = db_updated.astimezone(timezone.utc)
            last_confirmed_at = _serialize_optional_datetime(db_updated)
            evidence_note = (
                "last_confirmed_at uses production execution updated_at as cadence anchor"
            )
        else:
            evidence_note = (
                "last confirmed execution found but updated_at missing; "
                "supply --last-cycle-at or --override-cadence-check"
            )

    next_eligible_at: str | None = None
    cadence_eligible = False
    refusal: str | None = None

    if override_cadence_check:
        cadence_eligible = True
        if not str(override_cadence_reason or "").strip():
            refusal = "override_cadence_check requires --override-cadence-reason"
            cadence_eligible = False
    elif anchor is None:
        cadence_eligible = True
        evidence_note = (
            (evidence_note or "")
            + ("; " if evidence_note else "")
            + "no cadence anchor available; treating as first eligible periodic cycle"
        ).strip("; ")
    else:
        next_dt = anchor.astimezone(timezone.utc) + timedelta(minutes=interval)
        next_eligible_at = _serialize_datetime(next_dt)
        cadence_eligible = now.astimezone(timezone.utc) >= next_dt
        if not cadence_eligible:
            refusal = (
                f"cadence interval not elapsed; next eligible at {next_eligible_at} "
                f"(interval={interval} minutes)"
            )

    return CadenceEligibility(
        payout_cadence_policy=PAYOUT_CADENCE_POLICY,
        immediate_payout_allowed=IMMEDIATE_PAYOUT_ALLOWED,
        cycle_interval_minutes=interval,
        last_closed_execution_id=last_closed_execution_id,
        last_closed_at=last_confirmed_at,
        last_confirmed_at=last_confirmed_at,
        next_eligible_at=next_eligible_at,
        cadence_eligible=cadence_eligible,
        cadence_refusal_reason=refusal,
        cadence_evidence_note=evidence_note,
    )


def evaluate_idempotency_state(
    *,
    payout_plan_id: int,
    idempotency_key: str,
    plan_executions: list[Mapping[str, Any]],
) -> IdempotencyAssessment:
    key = production_executor.normalize_idempotency_key(idempotency_key)
    existing: Mapping[str, Any] | None = None
    blocking: Mapping[str, Any] | None = None

    for row in plan_executions:
        row_key = str(row.get("idempotency_key") or "")
        status = str(row.get("status") or "")
        if row_key == key:
            existing = row
        if status in _BLOCKING_PLAN_STATUSES and blocking is None:
            blocking = row

    existing_id = int(existing["id"]) if existing is not None else None
    existing_status = str(existing.get("status")) if existing is not None else None
    blocking_id = int(blocking["id"]) if blocking is not None else None
    blocking_status = str(blocking.get("status")) if blocking is not None else None

    refusal: str | None = None
    may_execute = True

    if existing is not None and existing_status in _IDEMPOTENT_COMPLETE_STATUSES:
        may_execute = False
        refusal = (
            f"idempotent replay: execution {existing_id} already {existing_status}; "
            "will not send again"
        )
    elif existing is not None and existing_status in _AUTO_RETRY_FORBIDDEN_STATUSES:
        may_execute = False
        refusal = (
            f"execution {existing_id} is {existing_status}; "
            "automatic retry is forbidden"
        )
    elif blocking is not None and (
        existing is None or int(blocking["id"]) != int(existing["id"])
    ):
        may_execute = False
        refusal = (
            f"plan {payout_plan_id} has blocking execution {blocking_id} "
            f"with status {blocking_status}"
        )

    return IdempotencyAssessment(
        idempotency_key=key,
        existing_execution_id=existing_id,
        existing_execution_status=existing_status,
        plan_has_blocking_execution=blocking is not None,
        blocking_execution_id=blocking_id,
        blocking_execution_status=blocking_status,
        may_execute=may_execute,
        refusal_reason=refusal,
    )


def _preflight_ready(preflight: Mapping[str, Any] | None) -> bool:
    if preflight is None:
        return False
    if str(preflight.get("preflight_status")) != production_preflight.PREFLIGHT_STATUS_PASSED:
        return False
    return bool(preflight.get("execution_allowed"))


def evaluate_readiness_gate(
    *,
    summary: Mapping[str, Any],
    active_chunked_reconciliation_count: int,
    preflight: Mapping[str, Any] | None,
) -> tuple[str, str | None]:
    report = cycle_readiness.evaluate_payout_cycle_readiness(
        summary=summary,
        active_chunked_reconciliation_count=active_chunked_reconciliation_count,
        preflight=preflight,
    )
    verdict = str(report.get("verdict"))
    if verdict in {
        cycle_readiness.VERDICT_HALT,
        cycle_readiness.VERDICT_NEEDS_EVIDENCE,
    }:
        reasons: list[str] = []
        halt = report.get("halt_reasons")
        missing = report.get("missing_evidence_reasons")
        if isinstance(halt, list):
            reasons.extend(str(item) for item in halt)
        if isinstance(missing, list):
            reasons.extend(str(item) for item in missing)
        detail = "; ".join(reasons) if reasons else f"readiness verdict is {verdict}"
        return verdict, detail
    return verdict, None


def evaluate_runner_gates(
    *,
    cadence: CadenceEligibility,
    idempotency: IdempotencyAssessment,
    preflight: Mapping[str, Any] | None,
    recommended_execution_mode: str | None,
    runner_approval_phrase: str | None = None,
    require_runner_approval: bool = False,
    readiness_verdict: str | None = None,
    readiness_refusal_reason: str | None = None,
) -> RunnerGateResult:
    refusal_parts: list[str] = []
    runner_approval_verified = False

    if require_runner_approval:
        if runner_approval_phrase is None:
            refusal_parts.append("runner approval phrase is required")
        elif not verify_runner_approval_phrase(runner_approval_phrase):
            refusal_parts.append(
                f"runner approval phrase must exactly match {RUNNER_APPROVAL_PHRASE!r}"
            )
        else:
            runner_approval_verified = True

    if not cadence.cadence_eligible and cadence.cadence_refusal_reason:
        refusal_parts.append(cadence.cadence_refusal_reason)

    if not idempotency.may_execute and idempotency.refusal_reason:
        refusal_parts.append(idempotency.refusal_reason)

    if readiness_verdict in {
        cycle_readiness.VERDICT_HALT,
        cycle_readiness.VERDICT_NEEDS_EVIDENCE,
    }:
        refusal_parts.append(
            readiness_refusal_reason
            or f"readiness verdict is {readiness_verdict}"
        )

    preflight_ok = _preflight_ready(preflight)
    if preflight is None:
        refusal_parts.append("production preflight not found")
    elif not preflight_ok:
        refusal_parts.append(
            "production preflight must be passed with execution_allowed=true"
        )

    mode = None
    if recommended_execution_mode is not None:
        mode = normalize_recommended_execution_mode(recommended_execution_mode)
        if mode == EXECUTOR_MODE_HALT:
            refusal_parts.append(
                "preflight recommended_execution_mode is halt; execution refused"
            )

    allowed = not refusal_parts
    return RunnerGateResult(
        allowed=allowed,
        refusal_reason="; ".join(refusal_parts) if refusal_parts else None,
        cadence=cadence,
        idempotency=idempotency,
        readiness_verdict=readiness_verdict,
        readiness_refusal_reason=readiness_refusal_reason,
        preflight_execution_allowed=preflight_ok if preflight is not None else None,
        recommended_execution_mode=mode,
        runner_approval_verified=runner_approval_verified,
    )


def build_single_executor_delegate_argv(
    *,
    python_executable: str,
    repo_script_path: str,
    payout_plan_id: int,
    production_preflight_id: int,
    source_wallet_name: str,
    azc_bin: str,
    idempotency_key: str,
    executor_confirm_phrase: str,
) -> list[str]:
    assert_no_forbidden_runner_wallet_keywords(python_executable)
    assert_no_forbidden_runner_wallet_keywords(repo_script_path)
    assert_no_forbidden_runner_wallet_keywords(azc_bin)
    assert_no_forbidden_runner_wallet_keywords(source_wallet_name)
    assert_no_forbidden_runner_wallet_keywords(idempotency_key)
    assert_no_forbidden_runner_wallet_keywords(executor_confirm_phrase)
    argv = [
        python_executable,
        repo_script_path,
        "execute-real",
        "--payout-plan-id",
        str(int(payout_plan_id)),
        "--production-preflight-id",
        str(int(production_preflight_id)),
        "--source-wallet-name",
        production_executor.normalize_source_wallet_name(source_wallet_name),
        "--azc-bin",
        azc_bin,
        "--idempotency-key",
        production_executor.normalize_idempotency_key(idempotency_key),
        "--confirm-phrase",
        production_executor.normalize_confirmation_phrase(executor_confirm_phrase),
    ]
    for arg in argv:
        assert_no_forbidden_runner_wallet_keywords(arg)
    return argv


def build_chunked_executor_delegate_argv(
    *,
    python_executable: str,
    repo_script_path: str,
    payout_plan_id: int,
    production_preflight_id: int,
    source_wallet_name: str,
    azc_bin: str,
    idempotency_key: str,
    executor_confirm_phrase: str,
    chunk_amount: str,
) -> list[str]:
    assert_no_forbidden_runner_wallet_keywords(python_executable)
    assert_no_forbidden_runner_wallet_keywords(repo_script_path)
    assert_no_forbidden_runner_wallet_keywords(azc_bin)
    assert_no_forbidden_runner_wallet_keywords(source_wallet_name)
    assert_no_forbidden_runner_wallet_keywords(idempotency_key)
    assert_no_forbidden_runner_wallet_keywords(executor_confirm_phrase)
    assert_no_forbidden_runner_wallet_keywords(chunk_amount)
    argv = [
        python_executable,
        repo_script_path,
        "execute-real",
        "--payout-plan-id",
        str(int(payout_plan_id)),
        "--production-preflight-id",
        str(int(production_preflight_id)),
        "--source-wallet-name",
        production_executor.normalize_source_wallet_name(source_wallet_name),
        "--chunk-amount",
        str(chunked_executor.normalize_chunk_amount(chunk_amount)),
        "--azc-bin",
        azc_bin,
        "--idempotency-key",
        production_executor.normalize_idempotency_key(idempotency_key),
        "--confirm-phrase",
        production_executor.normalize_confirmation_phrase(executor_confirm_phrase),
    ]
    for arg in argv:
        assert_no_forbidden_runner_wallet_keywords(arg)
    return argv


def cadence_eligibility_to_dict(cadence: CadenceEligibility) -> dict[str, Any]:
    return {
        "payout_cadence_policy": cadence.payout_cadence_policy,
        "immediate_payout_allowed": cadence.immediate_payout_allowed,
        "cycle_interval_minutes": cadence.cycle_interval_minutes,
        "last_closed_execution_id": cadence.last_closed_execution_id,
        "last_closed_at": cadence.last_closed_at,
        "last_confirmed_at": cadence.last_confirmed_at,
        "next_eligible_at": cadence.next_eligible_at,
        "cadence_eligible": cadence.cadence_eligible,
        "cadence_refusal_reason": cadence.cadence_refusal_reason,
        "cadence_evidence_note": cadence.cadence_evidence_note,
    }


def idempotency_assessment_to_dict(idempotency: IdempotencyAssessment) -> dict[str, Any]:
    return {
        "idempotency_key": idempotency.idempotency_key,
        "existing_execution_id": idempotency.existing_execution_id,
        "existing_execution_status": idempotency.existing_execution_status,
        "plan_has_blocking_execution": idempotency.plan_has_blocking_execution,
        "blocking_execution_id": idempotency.blocking_execution_id,
        "blocking_execution_status": idempotency.blocking_execution_status,
        "may_execute": idempotency.may_execute,
        "refusal_reason": idempotency.refusal_reason,
    }


def runner_gate_result_to_dict(gates: RunnerGateResult) -> dict[str, Any]:
    return {
        "allowed": gates.allowed,
        "refusal_reason": gates.refusal_reason,
        "cadence": cadence_eligibility_to_dict(gates.cadence),
        "idempotency": idempotency_assessment_to_dict(gates.idempotency),
        "readiness_verdict": gates.readiness_verdict,
        "readiness_refusal_reason": gates.readiness_refusal_reason,
        "preflight_execution_allowed": gates.preflight_execution_allowed,
        "recommended_execution_mode": gates.recommended_execution_mode,
        "runner_approval_verified": gates.runner_approval_verified,
        "runner_approval_phrase_required": RUNNER_APPROVAL_PHRASE,
        "accounting_note": (
            "manual-approved periodic runner coordinates existing tooling only; "
            "not unattended automation"
        ),
    }


def single_executor_script_relpath() -> str:
    return _SINGLE_EXECUTOR_SCRIPT


def chunked_executor_script_relpath() -> str:
    return _CHUNKED_EXECUTOR_SCRIPT
