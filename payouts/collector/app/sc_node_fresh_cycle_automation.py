from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from payouts.collector.app import sc_node_credit_ledger as credit_ledger
from payouts.collector.app import sc_node_manual_periodic_payout_runner as periodic_runner
from payouts.collector.app import sc_node_payout_plan_review as plan_review
from payouts.collector.app import sc_node_payout_planner as payout_planner
from payouts.collector.app import sc_node_payout_production_chunked_executor as chunked_executor
from payouts.collector.app import sc_node_payout_production_executor as production_executor
from payouts.collector.app import sc_node_payout_production_preflight as production_preflight
from payouts.collector.app import sc_node_payout_scheduler as payout_scheduler

DEFAULT_AUTOMATION_BASELINE = "2026-05-28T14:50:30+00:00"
DEFAULT_SCHEDULER_ENV_PATH = "/etc/azcoin-super/pool-ledger/payout-scheduler.env"
DEFAULT_SCHEDULER_ENV_FILE_MODE = 0o660
FRESH_CYCLE_AUTOMATION_NOTES = "fresh-cycle-automation"

MODE_PREVIEW = "preview"
MODE_WRITE_TARGET = "write-target"
MODE_EXECUTE_LIVE = "execute-live"

AUTOMATION_MODES = frozenset({MODE_PREVIEW, MODE_WRITE_TARGET, MODE_EXECUTE_LIVE})

ENV_BASELINE = "AZCOIN_FRESH_CYCLE_AUTOMATION_BASELINE"
ENV_MODE = "AZCOIN_FRESH_CYCLE_AUTOMATION_MODE"
ENV_WALLET = "AZCOIN_FRESH_CYCLE_AUTOMATION_WALLET"
ENV_RESERVE_FRACTION = "AZCOIN_FRESH_CYCLE_AUTOMATION_RESERVE_FRACTION"
ENV_TARGET_SINGLE_TX_MAX = "AZCOIN_FRESH_CYCLE_AUTOMATION_TARGET_SINGLE_TX_MAX_AMOUNT"
ENV_FALLBACK_CHUNK_AMOUNT = "AZCOIN_FRESH_CYCLE_AUTOMATION_FALLBACK_CHUNK_AMOUNT"
ENV_ENABLE_REAL_EXECUTION = "AZCOIN_FRESH_CYCLE_AUTOMATION_ENABLE_REAL_EXECUTION"
ENV_RUNNER_APPROVAL_PHRASE = "AZCOIN_FRESH_CYCLE_AUTOMATION_RUNNER_APPROVAL_PHRASE"
ENV_AZC_BIN = "AZCOIN_FRESH_CYCLE_AUTOMATION_AZC_BIN"
ENV_AZC_BIN_EXECUTE = "AZCOIN_FRESH_CYCLE_AUTOMATION_AZC_BIN_EXECUTE"
ENV_APPROVED_BY = "AZCOIN_FRESH_CYCLE_AUTOMATION_APPROVED_BY"
ENV_MIN_PAYOUT_AMOUNT = "AZCOIN_FRESH_CYCLE_AUTOMATION_MIN_PAYOUT_AMOUNT"

DEFAULT_AZC_BIN_READONLY = "/usr/local/bin/azc-payout-readonly"
DEFAULT_AZC_BIN_EXECUTE = "/usr/local/bin/azc-payout"

ENABLE_REAL_EXECUTION_TOKEN = "YES_ENABLE_FRESH_CYCLE_AUTOMATION"
RUNNER_APPROVAL_PHRASE = periodic_runner.RUNNER_APPROVAL_PHRASE

SAFE_SKIP_PREFIX = "SAFE_SKIP"
IDEMPOTENCY_PREFIX = "FRESH-CYCLE"

_SECRET_ENV_SUFFIXES = (
    "APPROVAL_PHRASE",
    "CONFIRM_PHRASE",
    "PASSWORD",
    "SECRET",
    "TOKEN",
    "DATABASE_URL",
    "IDEMPOTENCY_KEY",
)

_FORBIDDEN_AUTOMATION_WALLET_KEYWORDS = re.compile(
    r"\b("
    r"sendmany|sendtoaddress|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|importprivkey|importmulti|settxfee|bumpfee|"
    r"privkey|dumpwallet|azcoin-cli"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FreshCycleConfig:
    automation_baseline: datetime
    mode: str
    wallet_name: str
    reserve_fraction: Decimal
    target_single_tx_max_amount: Decimal
    fallback_chunk_amount: Decimal
    enable_real_execution: bool
    runner_approval_phrase: str | None
    azc_bin: str
    approved_by: str
    scheduler_env_path: str
    min_payout_amount: Decimal | None


@dataclass(frozen=True)
class FreshCycleSelection:
    automation_baseline: datetime
    latest_credit_run_coverage_end: datetime | None
    coverage_start: datetime
    coverage_end: datetime
    exclude_coverage_start_boundary: bool
    fresh_reward_events: tuple[Mapping[str, Any], ...]
    event_count: int
    amount_total: Decimal
    historical_backlog_count: int
    historical_backlog_amount: Decimal


@dataclass(frozen=True)
class FreshCycleExecutionPlan:
    recommended_execution_mode: str
    chunk_amount: Decimal | None
    expected_chunk_count: int | None
    executor_confirm_phrase: str | None
    refusal_reason: str | None = None


@dataclass(frozen=True)
class FreshCycleArtifactLookup:
    credit_run_id: int | None
    payout_plan_id: int | None
    production_preflight_id: int | None
    resume_note: str | None = None

    @property
    def is_complete(self) -> bool:
        return (
            self.credit_run_id is not None
            and self.payout_plan_id is not None
            and self.production_preflight_id is not None
        )

    @property
    def has_partial_credit_run(self) -> bool:
        return self.credit_run_id is not None and self.payout_plan_id is None


def assert_no_forbidden_automation_wallet_keywords(text: str) -> None:
    if _FORBIDDEN_AUTOMATION_WALLET_KEYWORDS.search(text):
        raise ValueError("automation text must not contain wallet send or signing keywords")


def format_safe_skip_message(reason: str) -> str:
    return f"{SAFE_SKIP_PREFIX}: {reason}"


def parse_automation_baseline(value: str | None) -> datetime:
    raw = str(value or DEFAULT_AUTOMATION_BASELINE).strip()
    if not raw:
        raise ValueError(f"{ENV_BASELINE} is required")
    return credit_ledger.parse_coverage_timestamp(raw, field_name=ENV_BASELINE)


def normalize_automation_mode(value: str | None, *, default: str = MODE_PREVIEW) -> str:
    mode = str(value or default).strip().lower()
    if mode not in AUTOMATION_MODES:
        raise ValueError(
            f"{ENV_MODE} must be one of: {', '.join(sorted(AUTOMATION_MODES))}"
        )
    return mode


def verify_enable_real_execution_flag(value: str | None) -> bool:
    return str(value or "").strip() == ENABLE_REAL_EXECUTION_TOKEN


def verify_runner_approval_phrase(value: str | None) -> bool:
    return str(value or "").strip() == RUNNER_APPROVAL_PHRASE


def resolve_azc_bin(explicit: str | None = None) -> str:
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    env_value = os.environ.get(ENV_AZC_BIN, "").strip()
    if env_value:
        return env_value
    legacy_azc = os.environ.get("AZC_BIN", "").strip()
    if legacy_azc and legacy_azc != "azc":
        return legacy_azc
    return DEFAULT_AZC_BIN_READONLY


def resolve_azc_bin_for_execute_live(explicit: str | None = None) -> str:
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    env_value = os.environ.get(ENV_AZC_BIN_EXECUTE, "").strip()
    if env_value:
        return env_value
    legacy_azc = os.environ.get("AZC_BIN", "").strip()
    if legacy_azc and legacy_azc not in ("azc", DEFAULT_AZC_BIN_READONLY):
        return legacy_azc
    return DEFAULT_AZC_BIN_EXECUTE


def parse_optional_min_payout_amount(value: str | None) -> Decimal | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return payout_planner.parse_decimal_amount(raw, field_name=ENV_MIN_PAYOUT_AMOUNT)


def evaluate_min_payout_refusal(
    *,
    planned_amount_total: Decimal,
    min_payout_amount: Decimal | None,
) -> str | None:
    if min_payout_amount is None:
        return None
    if planned_amount_total < min_payout_amount:
        return (
            "planned_amount_total "
            f"{payout_planner._serialize_numeric(planned_amount_total)} "
            "below "
            f"{ENV_MIN_PAYOUT_AMOUNT}="
            f"{payout_planner._serialize_numeric(min_payout_amount)}"
        )
    return None


def load_config_from_env(
    *,
    mode_override: str | None = None,
    scheduler_env_path: str | None = None,
) -> FreshCycleConfig:
    baseline = parse_automation_baseline(os.environ.get(ENV_BASELINE))
    mode = normalize_automation_mode(
        mode_override if mode_override is not None else os.environ.get(ENV_MODE)
    )
    wallet_name = credit_ledger.normalize_wallet_name(
        os.environ.get(ENV_WALLET, "wallet")
    )
    reserve_fraction = payout_planner.parse_reserve_fraction(
        os.environ.get(ENV_RESERVE_FRACTION, str(payout_planner.DEFAULT_RESERVE_FRACTION))
    )
    target_single_tx_max = payout_planner.parse_decimal_amount(
        os.environ.get(
            ENV_TARGET_SINGLE_TX_MAX,
            str(production_preflight.DEFAULT_TARGET_SINGLE_TX_MAX_AMOUNT),
        ),
        field_name=ENV_TARGET_SINGLE_TX_MAX,
    )
    fallback_chunk_amount = payout_planner.parse_decimal_amount(
        os.environ.get(
            ENV_FALLBACK_CHUNK_AMOUNT,
            str(production_preflight.DEFAULT_FALLBACK_CHUNK_AMOUNT),
        ),
        field_name=ENV_FALLBACK_CHUNK_AMOUNT,
    )
    enable_real_execution = verify_enable_real_execution_flag(
        os.environ.get(ENV_ENABLE_REAL_EXECUTION)
    )
    runner_phrase = os.environ.get(ENV_RUNNER_APPROVAL_PHRASE, "").strip() or None
    azc_bin = resolve_azc_bin()
    approved_by = os.environ.get(ENV_APPROVED_BY, "fresh-cycle-automation").strip()
    if not approved_by:
        approved_by = "fresh-cycle-automation"
    min_payout_amount = parse_optional_min_payout_amount(
        os.environ.get(ENV_MIN_PAYOUT_AMOUNT)
    )
    return FreshCycleConfig(
        automation_baseline=baseline,
        mode=mode,
        wallet_name=wallet_name,
        reserve_fraction=reserve_fraction,
        target_single_tx_max_amount=target_single_tx_max,
        fallback_chunk_amount=fallback_chunk_amount,
        enable_real_execution=enable_real_execution,
        runner_approval_phrase=runner_phrase,
        azc_bin=azc_bin,
        approved_by=approved_by,
        scheduler_env_path=scheduler_env_path or DEFAULT_SCHEDULER_ENV_PATH,
        min_payout_amount=min_payout_amount,
    )


def build_latest_credit_run_coverage_end_sql() -> str:
    sql = """
SELECT MAX(coverage_end) AS latest_coverage_end
FROM sc_node_reward_credit_runs
WHERE wallet_name = %(wallet_name)s
""".strip()
    credit_ledger._assert_readonly_sql(sql)
    return sql


def build_fresh_cycle_credit_run_for_coverage_sql() -> str:
    sql = """
SELECT id, status
FROM sc_node_reward_credit_runs
WHERE wallet_name = %(wallet_name)s
  AND coverage_start = %(coverage_start)s
  AND coverage_end = %(coverage_end)s
  AND notes = %(notes)s
ORDER BY id DESC
LIMIT 1
""".strip()
    credit_ledger._assert_readonly_sql(sql)
    return sql


def build_fresh_cycle_payout_plan_for_credit_run_sql() -> str:
    sql = """
SELECT id, status
FROM sc_node_payout_plans
WHERE credit_run_id = %(credit_run_id)s
ORDER BY id DESC
LIMIT 1
""".strip()
    credit_ledger._assert_readonly_sql(sql)
    return sql


def build_payout_plan_row_insert_params(
    *,
    payout_plan_id: int,
    row: payout_planner.PayoutPlanRowPreview,
    row_status: str = "draft",
) -> dict[str, Any]:
    return {
        "payout_plan_id": payout_plan_id,
        "credit_id": row.credit_id,
        "sc_node_id": row.sc_node_id,
        "sc_node_display_name": row.sc_node_display_name,
        "payout_address": row.payout_address,
        "gross_credit_amount": row.gross_credit_amount,
        "correction_amount": row.correction_amount,
        "payout_amount": row.payout_amount,
        "row_status": row_status,
    }


def required_payout_plan_row_insert_param_names() -> frozenset[str]:
    return frozenset(build_payout_plan_row_insert_params(
        payout_plan_id=0,
        row=payout_planner.PayoutPlanRowPreview(
            credit_id=0,
            sc_node_id="node",
            sc_node_display_name="Node",
            payout_address="addr",
            gross_credit_amount=Decimal("0"),
            correction_amount=Decimal("0"),
            payout_amount=Decimal("0"),
        ),
    ).keys())


def evaluate_partial_artifact_refusal(
    *,
    lookup: FreshCycleArtifactLookup,
    selection: FreshCycleSelection,
) -> str | None:
    if not lookup.has_partial_credit_run:
        return None
    return (
        f"fresh-cycle credit_run_id={lookup.credit_run_id} already exists for coverage "
        f"{_serialize_datetime(selection.coverage_start)}.."
        f"{_serialize_datetime(selection.coverage_end)} without payout plan; "
        "resuming payout plan write (no duplicate credit run)"
    )


def build_unlinked_mature_reward_events_sql() -> str:
    sql = """
SELECT
  r.id AS reward_event_id,
  r.txid,
  r.amount,
  r.event_time,
  r.maturity_status
FROM support_wallet_reward_events r
WHERE r.wallet_name = %(wallet_name)s
  AND r.maturity_status = 'mature'
  AND r.event_time IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM sc_node_reward_credit_run_events e
    WHERE e.reward_event_id = r.id
  )
ORDER BY r.event_time, r.id
""".strip()
    credit_ledger._assert_readonly_sql(sql)
    return sql


def compute_coverage_start(
    *,
    automation_baseline: datetime,
    latest_credit_run_coverage_end: datetime | None,
) -> datetime:
    if latest_credit_run_coverage_end is None:
        return automation_baseline
    return max(automation_baseline, latest_credit_run_coverage_end)


def compute_coverage_end_for_events(
    fresh_events: list[Mapping[str, Any]],
) -> datetime:
    if not fresh_events:
        raise ValueError("fresh_events must not be empty")
    latest = max(_event_time(row) for row in fresh_events)
    return latest + timedelta(microseconds=1)


def reward_event_is_fresh(
    event_time: datetime,
    *,
    coverage_start: datetime,
    exclude_coverage_start_boundary: bool,
    coverage_end: datetime,
) -> bool:
    return credit_ledger.reward_event_time_in_coverage(
        event_time,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        exclude_coverage_start_boundary=exclude_coverage_start_boundary,
    )


def select_fresh_reward_events(
    unlinked_events: list[Mapping[str, Any]],
    *,
    coverage_start: datetime,
    coverage_end: datetime,
    exclude_coverage_start_boundary: bool,
) -> list[Mapping[str, Any]]:
    selected: list[Mapping[str, Any]] = []
    for row in unlinked_events:
        event_time = _event_time(row)
        if event_time < coverage_start:
            continue
        if reward_event_is_fresh(
            event_time,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            exclude_coverage_start_boundary=exclude_coverage_start_boundary,
        ):
            selected.append(row)
    return selected


def summarize_historical_backlog(
    unlinked_events: list[Mapping[str, Any]],
    *,
    automation_baseline: datetime,
) -> tuple[int, Decimal]:
    count = 0
    total = Decimal("0")
    for row in unlinked_events:
        if _event_time(row) < automation_baseline:
            count += 1
            total += _to_decimal(row.get("amount"))
    return count, total


def build_fresh_cycle_selection(
    *,
    config: FreshCycleConfig,
    unlinked_events: list[Mapping[str, Any]],
    latest_credit_run_coverage_end: datetime | None,
    exclude_coverage_start_boundary: bool,
) -> FreshCycleSelection | None:
    coverage_start = compute_coverage_start(
        automation_baseline=config.automation_baseline,
        latest_credit_run_coverage_end=latest_credit_run_coverage_end,
    )
    candidate_events = [
        row
        for row in unlinked_events
        if _event_time(row) >= coverage_start
    ]
    if not candidate_events:
        return None

    provisional_end = compute_coverage_end_for_events(candidate_events)
    fresh_events = select_fresh_reward_events(
        candidate_events,
        coverage_start=coverage_start,
        coverage_end=provisional_end,
        exclude_coverage_start_boundary=exclude_coverage_start_boundary,
    )
    if not fresh_events:
        return None

    coverage_end = compute_coverage_end_for_events(fresh_events)
    fresh_events = select_fresh_reward_events(
        fresh_events,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        exclude_coverage_start_boundary=exclude_coverage_start_boundary,
    )
    if not fresh_events:
        return None

    amount_total = sum((_to_decimal(row.get("amount")) for row in fresh_events), Decimal("0"))
    historical_count, historical_amount = summarize_historical_backlog(
        unlinked_events,
        automation_baseline=config.automation_baseline,
    )
    return FreshCycleSelection(
        automation_baseline=config.automation_baseline,
        latest_credit_run_coverage_end=latest_credit_run_coverage_end,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        exclude_coverage_start_boundary=exclude_coverage_start_boundary,
        fresh_reward_events=tuple(fresh_events),
        event_count=len(fresh_events),
        amount_total=amount_total,
        historical_backlog_count=historical_count,
        historical_backlog_amount=historical_amount,
    )


def build_credit_coverage(selection: FreshCycleSelection) -> credit_ledger.CreditCoverage:
    return credit_ledger.resolve_operator_coverage(
        coverage_start=selection.coverage_start,
        coverage_end=selection.coverage_end,
        pool_coverage_start=None,
        pool_coverage_end=None,
        reward_coverage_start=None,
        reward_coverage_end=None,
    )


def build_execution_plan(
    *,
    preflight_preview: production_preflight.ProductionPayoutPreflightPreview,
    payout_plan_id: int,
    source_wallet_name: str,
    min_payout_refusal: str | None = None,
) -> FreshCycleExecutionPlan:
    policy = preflight_preview.utxo_chunking_policy
    mode = policy.recommended_execution_mode
    chunk_amount: Decimal | None = None
    expected_chunk_count: int | None = None
    executor_phrase: str | None = None
    if min_payout_refusal is not None:
        mode = production_preflight.RECOMMENDED_EXECUTION_MODE_HALT
    elif mode == production_preflight.RECOMMENDED_EXECUTION_MODE_SINGLE:
        executor_phrase = production_executor.build_expected_confirmation_phrase(
            payout_plan_id,
            preflight_preview.planned_amount_total,
            source_wallet_name,
        )
    elif mode == production_preflight.RECOMMENDED_EXECUTION_MODE_CHUNKED:
        chunk_amount = policy.recommended_chunk_size
        expected_chunk_count = policy.estimated_chunk_count
        executor_phrase = chunked_executor.build_chunked_confirmation_phrase(
            payout_plan_id=payout_plan_id,
            planned_amount_total=preflight_preview.planned_amount_total,
            source_wallet_name=source_wallet_name,
            chunk_count=expected_chunk_count,
        )
    refusal_reason = resolve_execution_refusal_reason(
        preflight_preview=preflight_preview,
        recommended_execution_mode=mode,
        min_payout_refusal=min_payout_refusal,
    )
    return FreshCycleExecutionPlan(
        recommended_execution_mode=mode,
        chunk_amount=chunk_amount,
        expected_chunk_count=expected_chunk_count,
        executor_confirm_phrase=executor_phrase,
        refusal_reason=refusal_reason,
    )


def resolve_execution_refusal_reason(
    *,
    preflight_preview: production_preflight.ProductionPayoutPreflightPreview,
    recommended_execution_mode: str,
    min_payout_refusal: str | None = None,
) -> str | None:
    if recommended_execution_mode != production_preflight.RECOMMENDED_EXECUTION_MODE_HALT:
        return preflight_preview.refusal_reason
    parts: list[str] = []
    if min_payout_refusal:
        parts.append(min_payout_refusal)
    if preflight_preview.refusal_reason:
        parts.append(preflight_preview.refusal_reason)
    policy = preflight_preview.utxo_chunking_policy
    if policy.refusal_reason:
        parts.append(policy.refusal_reason)
    if policy.utxo_evidence_note:
        parts.append(policy.utxo_evidence_note)
    if not parts:
        return (
            "recommended_execution_mode=halt without explicit preflight refusal "
            "(check wallet balance, reserve policy, and payout plan rows)"
        )
    return "; ".join(parts)


def build_hypothetical_plan_preview(
    *,
    credit_preview: credit_ledger.CreditRunPreview,
    wallet_name: str,
    reserve_fraction: Decimal,
    trusted_balance_snapshot: Decimal,
    address_lookup: dict[str, list[Mapping[str, Any]]],
) -> payout_planner.PayoutPlanPreview:
    credit_run: dict[str, Any] = {
        "id": 0,
        "wallet_name": wallet_name,
        "maturity_status": credit_ledger.CREDIT_MATURITY_STATUS,
        "status": "draft",
        "reward_amount_total": credit_preview.reward_amount_total,
    }
    credits: list[dict[str, Any]] = [
        {
            "id": index + 1,
            "credit_run_id": 0,
            "sc_node_id": credit.sc_node_id,
            "sc_node_display_name": credit.sc_node_display_name,
            "credit_amount": credit.credit_amount,
            "credit_status": "draft",
        }
        for index, credit in enumerate(credit_preview.sc_node_credits)
    ]
    return payout_planner.build_payout_plan_preview(
        credit_run_id=0,
        wallet_name=wallet_name,
        reserve_fraction=reserve_fraction,
        trusted_balance_snapshot=trusted_balance_snapshot,
        credit_run=credit_run,
        credits=credits,
        address_lookup=address_lookup,
        existing_draft_plan_id=None,
    )


def build_hypothetical_preflight_preview(
    *,
    plan_preview: payout_planner.PayoutPlanPreview,
    wallet_name: str,
    wallet_balance: production_preflight.WalletBalance,
    utxo_snapshot: production_preflight.UtxoSnapshot,
    config: FreshCycleConfig,
) -> production_preflight.ProductionPayoutPreflightPreview:
    plan_rows: list[dict[str, Any]] = [
        {
            "id": 0,
            "sc_node_id": row.sc_node_id,
            "payout_address": row.payout_address,
            "payout_amount": row.payout_amount,
            "row_status": plan_review.ROW_STATUS_APPROVED,
        }
        for row in plan_preview.rows
    ]
    plan: dict[str, Any] = {
        "id": 0,
        "status": plan_review.PLAN_STATUS_APPROVED,
        "planned_amount_total": plan_preview.planned_amount_total,
    }
    return production_preflight.build_production_preflight_preview(
        payout_plan_id=0,
        source_wallet_name=production_preflight.normalize_source_wallet_name(wallet_name),
        plan=plan,
        plan_rows=plan_rows,
        wallet_balance=wallet_balance,
        address_lookup={
            row.sc_node_id: [{"payout_address": row.payout_address}]
            for row in plan_preview.rows
        },
        operator_override=False,
        reserve_percent=config.reserve_fraction,
        reserve_amount=None,
        max_spend_percent=production_preflight.DEFAULT_MAX_SPEND_PERCENT,
        reserve_mode=production_preflight.RESERVE_MODE_PERCENT,
        utxo_snapshot=utxo_snapshot,
        target_single_tx_max_amount=config.target_single_tx_max_amount,
        fallback_chunk_amount=config.fallback_chunk_amount,
    )


def preflight_preview_fields(
    preflight_preview: production_preflight.ProductionPayoutPreflightPreview,
) -> dict[str, Any]:
    policy = preflight_preview.utxo_chunking_policy
    return {
        "preflight_status": (
            production_preflight.PREFLIGHT_STATUS_PASSED
            if preflight_preview.execution_allowed
            else production_preflight.PREFLIGHT_STATUS_REFUSED
        ),
        "execution_allowed": preflight_preview.execution_allowed,
        "spendable_after_reserve": payout_planner._serialize_numeric(
            preflight_preview.spendable_after_reserve
        ),
        "reserve_amount": payout_planner._serialize_numeric(
            preflight_preview.reserve_amount
        ),
        "trusted_balance": payout_planner._serialize_numeric(
            preflight_preview.wallet_balance.trusted
        ),
        "wallet_balance_source": production_preflight.WALLET_BALANCE_SOURCE_AZC_GETBALANCES,
        "utxo_chunking_policy": production_preflight.utxo_chunking_policy_to_dict(policy),
    }


def build_preflight_idempotency_key(*, credit_run_id: int) -> str:
    return f"{IDEMPOTENCY_PREFIX}-PREFLIGHT-CREDIT-RUN-{int(credit_run_id)}-V1"


def build_execution_idempotency_key(
    *,
    credit_run_id: int,
    payout_plan_id: int,
    production_preflight_id: int,
) -> str:
    return (
        f"{IDEMPOTENCY_PREFIX}-{int(credit_run_id)}-PLAN-{int(payout_plan_id)}-"
        f"PREFLIGHT-{int(production_preflight_id)}-EXECUTE-V1"
    )


def build_credit_run_label(*, credit_run_id: int | None = None) -> str:
    if credit_run_id is None:
        return "fresh-cycle-automation"
    return f"fresh-cycle-automation-{int(credit_run_id)}"


def build_scheduler_target_env_lines(
    *,
    payout_plan_id: int,
    production_preflight_id: int,
    recommended_execution_mode: str,
    source_wallet_name: str,
    chunk_amount: Decimal | None = None,
    mode: str = payout_scheduler.MODE_REPORT_ONLY,
) -> list[str]:
    lines = [
        "# AZCoin SC-node payout scheduler target (managed by fresh-cycle automation)",
        f"SC_NODE_PAYOUT_SCHEDULER_MODE={mode}",
        f"SC_NODE_PAYOUT_SCHEDULER_PAYOUT_PLAN_ID={int(payout_plan_id)}",
        f"SC_NODE_PAYOUT_SCHEDULER_PRODUCTION_PREFLIGHT_ID={int(production_preflight_id)}",
        f"SC_NODE_PAYOUT_SCHEDULER_RECOMMENDED_EXECUTION_MODE={recommended_execution_mode}",
        f"SC_NODE_PAYOUT_SCHEDULER_SOURCE_WALLET_NAME={source_wallet_name}",
    ]
    if chunk_amount is not None:
        lines.append(
            "SC_NODE_PAYOUT_SCHEDULER_CHUNK_AMOUNT="
            f"{payout_planner._serialize_numeric(chunk_amount)}"
        )
    return lines


def build_safe_skip_scheduler_env_lines() -> list[str]:
    return [
        "# AZCoin SC-node payout scheduler (safe-skip default)",
        f"SC_NODE_PAYOUT_SCHEDULER_MODE={payout_scheduler.MODE_REPORT_ONLY}",
    ]


def render_scheduler_env_content(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def write_scheduler_env_file(
    path: str,
    lines: list[str],
    *,
    file_mode: int = DEFAULT_SCHEDULER_ENV_FILE_MODE,
) -> None:
    content = render_scheduler_env_content(lines)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(tmp, file_mode)
        os.replace(tmp, target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def evaluate_execute_live_refusal(config: FreshCycleConfig) -> str | None:
    if config.mode != MODE_EXECUTE_LIVE:
        return None
    if not config.enable_real_execution:
        return (
            f"execute-live requires {ENV_ENABLE_REAL_EXECUTION}="
            f"{ENABLE_REAL_EXECUTION_TOKEN}"
        )
    if not verify_runner_approval_phrase(config.runner_approval_phrase):
        return (
            f"execute-live requires {ENV_RUNNER_APPROVAL_PHRASE}="
            f"{RUNNER_APPROVAL_PHRASE}"
        )
    return None


def build_preview_summary(
    *,
    config: FreshCycleConfig,
    selection: FreshCycleSelection | None,
    credit_preview: credit_ledger.CreditRunPreview | None,
    preflight_preview: production_preflight.ProductionPayoutPreflightPreview | None = None,
    execution_plan: FreshCycleExecutionPlan | None = None,
    would_write: bool = False,
    would_execute: bool = False,
    target_ids: Mapping[str, int | None] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": config.mode,
        "automation_baseline": _serialize_datetime(config.automation_baseline),
        "wallet_name": config.wallet_name,
        "would_write": would_write,
        "would_execute": would_execute,
        "accounting_note": (
            "fresh-cycle automation excludes historical backlog before baseline; "
            "never uses default coverage"
        ),
    }
    if selection is None:
        payload["safe_skip"] = True
        payload["message"] = format_safe_skip_message("no fresh mature rewards after baseline")
        payload["historical_backlog_count"] = 0
        payload["historical_backlog_amount"] = "0"
        return payload

    payload.update(
        {
            "coverage_start": _serialize_datetime(selection.coverage_start),
            "coverage_end": _serialize_datetime(selection.coverage_end),
            "latest_credit_run_coverage_end": (
                _serialize_datetime(selection.latest_credit_run_coverage_end)
                if selection.latest_credit_run_coverage_end is not None
                else None
            ),
            "exclude_coverage_start_boundary": selection.exclude_coverage_start_boundary,
            "event_count": selection.event_count,
            "amount_total": payout_planner._serialize_numeric(selection.amount_total),
            "historical_backlog_count": selection.historical_backlog_count,
            "historical_backlog_amount": payout_planner._serialize_numeric(
                selection.historical_backlog_amount
            ),
        }
    )
    if credit_preview is not None:
        payload["allocation_allowed"] = credit_preview.allocation_allowed
        payload["credit_refusal_reason"] = credit_preview.refusal_reason
        payload["mapped_work_total"] = payout_planner._serialize_numeric(
            credit_preview.mapped_work_total
        )
    if preflight_preview is not None:
        payload.update(preflight_preview_fields(preflight_preview))
    if execution_plan is not None:
        payload["recommended_execution_mode"] = execution_plan.recommended_execution_mode
        payload["expected_chunk_count"] = execution_plan.expected_chunk_count
        if execution_plan.chunk_amount is not None:
            payload["chunk_amount"] = payout_planner._serialize_numeric(
                execution_plan.chunk_amount
            )
        payload["refusal_reason"] = execution_plan.refusal_reason
        if (
            execution_plan.recommended_execution_mode
            == production_preflight.RECOMMENDED_EXECUTION_MODE_HALT
            and not execution_plan.refusal_reason
        ):
            payload["refusal_reason"] = resolve_execution_refusal_reason(
                preflight_preview=preflight_preview,
                recommended_execution_mode=execution_plan.recommended_execution_mode,
            ) if preflight_preview is not None else (
                "recommended_execution_mode=halt without preflight preview"
            )
    elif credit_preview is not None:
        payload["refusal_reason"] = credit_preview.refusal_reason
    if target_ids is not None:
        payload["target_ids"] = dict(target_ids)
    return payload


def build_manual_runner_execute_argv(
    *,
    python_executable: str,
    repo_root: str,
    payout_plan_id: int,
    production_preflight_id: int,
    recommended_execution_mode: str,
    idempotency_key: str,
    source_wallet_name: str,
    azc_bin: str,
    runner_approval_phrase: str,
    executor_confirm_phrase: str,
    chunk_amount: Decimal | None = None,
) -> list[str]:
    argv = payout_scheduler.build_manual_runner_delegate_argv(
        python_executable=python_executable,
        repo_root=repo_root,
        payout_plan_id=payout_plan_id,
        production_preflight_id=production_preflight_id,
        recommended_execution_mode=recommended_execution_mode,
        cycle_interval_minutes=periodic_runner.DEFAULT_CYCLE_INTERVAL_MINUTES,
        idempotency_key=idempotency_key,
        source_wallet_name=source_wallet_name,
        azc_bin=azc_bin,
        runner_approval_phrase=runner_approval_phrase,
        executor_confirm_phrase=executor_confirm_phrase,
        chunk_amount=chunk_amount,
        dry_run_delegate=False,
    )
    insert_at = argv.index("execute-approved") + 1
    argv[insert_at:insert_at] = [
        "--override-cadence-check",
        "--override-cadence-reason",
        "fresh-cycle-automation",
    ]
    return argv


def build_scheduler_delegate_argv(
    *,
    python_executable: str,
    repo_root: str,
    payout_plan_id: int,
    production_preflight_id: int,
    recommended_execution_mode: str,
    idempotency_key: str,
    source_wallet_name: str,
    azc_bin: str,
    runner_approval_phrase: str,
    executor_confirm_phrase: str,
    chunk_amount: Decimal | None = None,
    enable_real_execution: bool = True,
) -> list[str]:
    if not enable_real_execution:
        raise ValueError("scheduler delegation requires enable_real_execution")
    os.environ[payout_scheduler.ENV_RUNNER_APPROVAL_PHRASE] = runner_approval_phrase
    os.environ[payout_scheduler.ENV_EXECUTOR_CONFIRM_PHRASE] = executor_confirm_phrase
    argv = [
        python_executable,
        f"{repo_root.rstrip('/')}/payouts/scripts/sc_node_payout_scheduler.py",
        "--scheduler-mode",
        payout_scheduler.MODE_EXECUTE_ENABLED,
        "--enable-real-execution",
        payout_scheduler.ENABLE_REAL_EXECUTION_TOKEN,
        "--payout-plan-id",
        str(int(payout_plan_id)),
        "--production-preflight-id",
        str(int(production_preflight_id)),
        "--recommended-execution-mode",
        recommended_execution_mode,
        "--idempotency-key",
        production_executor.normalize_idempotency_key(idempotency_key),
        "--source-wallet-name",
        production_executor.normalize_source_wallet_name(source_wallet_name),
        "--azc-bin",
        azc_bin,
    ]
    if chunk_amount is not None:
        argv.extend(
            [
                "--chunk-amount",
                payout_planner._serialize_numeric(chunk_amount),
            ]
        )
    for arg in argv:
        assert_no_forbidden_automation_wallet_keywords(arg)
    return argv


def redact_secret_text(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        redacted = line
        for suffix in _SECRET_ENV_SUFFIXES:
            pattern = re.compile(rf"({suffix}\s*=\s*).+", re.IGNORECASE)
            redacted = pattern.sub(r"\1***REDACTED***", redacted)
        for flag in ("--idempotency-key", "--runner-approval-phrase"):
            redacted = re.sub(
                rf"({re.escape(flag)}\s+)\S+",
                r"\1***REDACTED***",
                redacted,
            )
        redacted = re.sub(
            r"(--executor-confirm-phrase\s+).+",
            r"\1***REDACTED***",
            redacted,
        )
        lines.append(redacted)
    return "\n".join(lines)


def build_sent_fresh_cycle_executions_sql() -> str:
    sql = """
SELECT
  id,
  payout_plan_id,
  production_preflight_id,
  source_wallet_name,
  status,
  planned_amount_total,
  txid,
  idempotency_key,
  notes,
  created_at,
  updated_at
FROM sc_node_payout_production_executions
WHERE status = 'sent'
  AND txid IS NOT NULL
  AND idempotency_key LIKE 'FRESH-CYCLE-%'
ORDER BY id ASC
""".strip()
    credit_ledger._assert_readonly_sql(sql)
    return sql


def build_confirm_sent_mark_confirmed_argv(
    *,
    python_executable: str,
    repo_root: str,
    production_execution_id: int,
    source_wallet_name: str,
    azc_bin: str,
    notes: str,
) -> list[str]:
    if chunked_executor.is_chunked_execution_notes(notes):
        return [
            python_executable,
            f"{repo_root.rstrip('/')}/payouts/scripts/sc_node_payout_production_chunked_executor.py",
            "mark-confirmed",
            "--production-execution-id",
            str(int(production_execution_id)),
        ]
    return [
        python_executable,
        f"{repo_root.rstrip('/')}/payouts/scripts/sc_node_payout_production_executor.py",
        "mark-confirmed",
        "--production-execution-id",
        str(int(production_execution_id)),
        "--confirm-chain-evidence",
        "--source-wallet-name",
        production_preflight.normalize_source_wallet_name(source_wallet_name),
        "--azc-bin",
        azc_bin,
    ]


def _event_time(row: Mapping[str, Any]) -> datetime:
    value = row.get("event_time")
    if not isinstance(value, datetime):
        raise ValueError("reward event_time must be datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _to_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
