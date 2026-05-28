#!/usr/bin/env python3
"""SC-node payout scheduler v0 (report-only default; delegates through PR Y runner)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psycopg
from psycopg.rows import dict_row

from payouts.collector.app import sc_node_manual_periodic_payout_runner as periodic_runner
from payouts.collector.app import sc_node_payout_scheduler as scheduler
from payouts.scripts import sc_node_manual_periodic_payout_runner as runner_cli


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "SC-node payout scheduler v0 (default report-only; delegates via PR Y runner)"
        )
    )
    parser.add_argument(
        "--scheduler-mode",
        default=None,
        choices=sorted(scheduler.SCHEDULER_MODES),
        help=(
            "report-only (default), dry-run-delegate, or execute-enabled "
            f"(env {scheduler.ENV_SCHEDULER_MODE})"
        ),
    )
    parser.add_argument(
        "--payout-plan-id",
        type=int,
        default=None,
        help=f"Explicit approved plan (env {scheduler.ENV_PAYOUT_PLAN_ID})",
    )
    parser.add_argument(
        "--production-preflight-id",
        type=int,
        default=None,
        help=f"Passed preflight row (env {scheduler.ENV_PRODUCTION_PREFLIGHT_ID})",
    )
    parser.add_argument(
        "--recommended-execution-mode",
        default=None,
        choices=["single", "chunked", "halt"],
        help=(
            "From PR X preflight preview "
            f"(env {scheduler.ENV_RECOMMENDED_EXECUTION_MODE})"
        ),
    )
    parser.add_argument(
        "--cycle-interval-minutes",
        type=int,
        default=None,
        help=(
            "Periodic cadence interval "
            f"(env {periodic_runner.ENV_CYCLE_INTERVAL_MINUTES} or default "
            f"{periodic_runner.DEFAULT_CYCLE_INTERVAL_MINUTES})"
        ),
    )
    parser.add_argument("--idempotency-key", default=None)
    parser.add_argument(
        "--readiness-production-execution-id",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--enable-real-execution",
        default=None,
        help=f"Required for execute-enabled: {scheduler.ENABLE_REAL_EXECUTION_TOKEN}",
    )
    parser.add_argument(
        "--source-wallet-name",
        default=None,
        help=f"Override env {scheduler.ENV_SOURCE_WALLET_NAME} for delegate modes",
    )
    parser.add_argument(
        "--azc-bin",
        default=None,
        help=f"Override env {scheduler.ENV_AZC_BIN} for execute-enabled",
    )
    parser.add_argument(
        "--chunk-amount",
        default=None,
        help=f"Override env {scheduler.ENV_CHUNK_AMOUNT} when mode=chunked",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser.parse_args(argv)


def _emit_safe_skip(reason: str, *, as_json: bool) -> int:
    message = scheduler.format_safe_skip_message(reason)
    if as_json:
        json.dump(
            {
                "safe_skip": True,
                "message": message,
                "accounting_note": (
                    "scheduler v0 requires explicit approved target IDs; "
                    "no auto-discovery"
                ),
            },
            sys.stdout,
            indent=2,
            sort_keys=True,
        )
        sys.stdout.write("\n")
    else:
        sys.stdout.write(message + "\n")
    return scheduler.EXIT_SUCCESS


def _database_url() -> str:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        raise SystemExit(scheduler.EXIT_USAGE_ERROR)
    return database_url


def _load_gate_payload(
    cur: psycopg.Cursor,
    *,
    args: argparse.Namespace,
    target: scheduler.SchedulerTargetConfig,
    interval: int,
    idempotency_key: str | None,
    require_runner_approval: bool,
    runner_approval_phrase: str | None,
) -> dict[str, Any]:
    return runner_cli._build_bundle(
        cur,
        payout_plan_id=target.payout_plan_id,
        production_preflight_id=target.production_preflight_id,
        cycle_interval_minutes=interval,
        last_cycle_at=None,
        override_cadence_check=False,
        override_cadence_reason=None,
        recommended_execution_mode=target.recommended_execution_mode,
        readiness_production_execution_id=args.readiness_production_execution_id,
        idempotency_key=idempotency_key,
        runner_approval_phrase=runner_approval_phrase,
        require_runner_approval=require_runner_approval,
    )


def _resolve_idempotency_key(args: argparse.Namespace, config: scheduler.SchedulerExecutionConfig) -> str | None:
    if args.idempotency_key is not None:
        return str(args.idempotency_key).strip() or None
    return config.idempotency_key


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    mode = scheduler.resolve_scheduler_mode(cli_value=args.scheduler_mode)
    target = scheduler.resolve_scheduler_target(
        payout_plan_id=args.payout_plan_id,
        production_preflight_id=args.production_preflight_id,
        recommended_execution_mode=args.recommended_execution_mode,
    )

    if target.config_error is not None:
        print(target.config_error, file=sys.stderr)
        return scheduler.EXIT_USAGE_ERROR

    if not target.explicit_target_configured:
        return _emit_safe_skip(
            "explicit payout target not configured "
            f"({scheduler.ENV_PAYOUT_PLAN_ID}, "
            f"{scheduler.ENV_PRODUCTION_PREFLIGHT_ID}, "
            f"{scheduler.ENV_RECOMMENDED_EXECUTION_MODE})",
            as_json=args.json,
        )

    if mode == scheduler.MODE_EXECUTE_ENABLED and not scheduler.verify_enable_real_execution_flag(
        args.enable_real_execution
    ):
        return _emit_safe_skip(
            f"execute-enabled requires --enable-real-execution "
            f"{scheduler.ENABLE_REAL_EXECUTION_TOKEN}",
            as_json=args.json,
        )

    interval = periodic_runner.parse_cycle_interval_minutes(cli_value=args.cycle_interval_minutes)
    exec_config = scheduler.load_execution_config(
        enable_real_execution_flag=args.enable_real_execution,
        source_wallet_name=args.source_wallet_name,
        azc_bin=args.azc_bin,
        idempotency_key=args.idempotency_key,
        chunk_amount=args.chunk_amount,
    )

    idempotency_key = _resolve_idempotency_key(args, exec_config)
    require_runner_approval = mode != scheduler.MODE_REPORT_ONLY
    runner_phrase = exec_config.runner_approval_phrase if require_runner_approval else None

    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        with conn.cursor(row_factory=dict_row) as cur:
            gate_payload = _load_gate_payload(
                cur,
                args=args,
                target=target,
                interval=interval,
                idempotency_key=idempotency_key,
                require_runner_approval=require_runner_approval,
                runner_approval_phrase=runner_phrase,
            )

    delegated_command: list[str] | None = None
    executed = False
    refusal: str | None = None

    if mode == scheduler.MODE_EXECUTE_ENABLED and exec_config.config_refusal_reason:
        refusal = exec_config.config_refusal_reason
    elif mode in {scheduler.MODE_DRY_RUN_DELEGATE, scheduler.MODE_EXECUTE_ENABLED}:
        if idempotency_key is None:
            refusal = "delegate modes require --idempotency-key or configured env idempotency key"
        elif exec_config.runner_approval_phrase is None:
            refusal = f"delegate modes require {scheduler.ENV_RUNNER_APPROVAL_PHRASE}"
        elif exec_config.executor_confirm_phrase is None:
            refusal = f"delegate modes require {scheduler.ENV_EXECUTOR_CONFIRM_PHRASE}"
        elif exec_config.source_wallet_name is None:
            refusal = f"delegate modes require {scheduler.ENV_SOURCE_WALLET_NAME}"
        elif (
            periodic_runner.normalize_recommended_execution_mode(
                target.recommended_execution_mode or "halt"
            )
            == periodic_runner.EXECUTOR_MODE_CHUNKED
            and exec_config.chunk_amount is None
        ):
            refusal = f"chunked mode requires --chunk-amount or {scheduler.ENV_CHUNK_AMOUNT}"

    gates_allowed = bool(gate_payload.get("gates", {}).get("allowed"))
    if (
        refusal is None
        and mode in {scheduler.MODE_DRY_RUN_DELEGATE, scheduler.MODE_EXECUTE_ENABLED}
        and gates_allowed
        and idempotency_key is not None
        and exec_config.runner_approval_phrase is not None
        and exec_config.executor_confirm_phrase is not None
        and exec_config.source_wallet_name is not None
        and target.payout_plan_id is not None
        and target.production_preflight_id is not None
        and target.recommended_execution_mode is not None
    ):
        delegated_command = scheduler.build_manual_runner_delegate_argv(
            python_executable=sys.executable,
            repo_root=str(REPO_ROOT),
            payout_plan_id=target.payout_plan_id,
            production_preflight_id=target.production_preflight_id,
            recommended_execution_mode=target.recommended_execution_mode,
            cycle_interval_minutes=interval,
            idempotency_key=idempotency_key,
            source_wallet_name=exec_config.source_wallet_name,
            azc_bin=exec_config.azc_bin or "/usr/local/bin/azc-payout",
            runner_approval_phrase=exec_config.runner_approval_phrase,
            executor_confirm_phrase=exec_config.executor_confirm_phrase,
            readiness_production_execution_id=args.readiness_production_execution_id,
            chunk_amount=exec_config.chunk_amount,
            dry_run_delegate=mode == scheduler.MODE_DRY_RUN_DELEGATE,
        )

        if mode == scheduler.MODE_DRY_RUN_DELEGATE:
            completed = subprocess.run(
                delegated_command,
                check=False,
                capture_output=True,
                text=True,
                shell=False,
                cwd=str(REPO_ROOT),
                env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            )
            if completed.returncode != 0:
                message = (
                    completed.stderr or completed.stdout or "delegated runner dry-run failed"
                ).strip()
                print(message, file=sys.stderr)
                refusal = message
        elif mode == scheduler.MODE_EXECUTE_ENABLED and exec_config.enable_real_execution:
            completed = subprocess.run(
                delegated_command,
                check=False,
                capture_output=True,
                text=True,
                shell=False,
                cwd=str(REPO_ROOT),
                env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            )
            if completed.returncode != 0:
                message = (
                    completed.stderr or completed.stdout or "delegated runner failed"
                ).strip()
                print(message, file=sys.stderr)
                refusal = message
            else:
                executed = True
                try:
                    runner_payload = json.loads(completed.stdout)
                    if not bool(runner_payload.get("executed")):
                        executed = bool(runner_payload.get("idempotent_replay"))
                except json.JSONDecodeError:
                    pass

    report = scheduler.build_scheduler_report(
        scheduler_mode=mode,
        payout_plan_id=target.payout_plan_id,
        production_preflight_id=target.production_preflight_id,
        recommended_execution_mode=target.recommended_execution_mode or "halt",
        gate_payload=gate_payload,
        execution_config=exec_config if mode == scheduler.MODE_EXECUTE_ENABLED else None,
        delegated_command=delegated_command,
        executed=executed,
        refusal_reason=refusal,
        now=datetime.now(timezone.utc),
    )

    if args.json:
        try:
            json.dump(scheduler.scheduler_report_to_dict(report), sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
        except BrokenPipeError:
            return scheduler.scheduler_exit_code(report)
    else:
        try:
            sys.stdout.write(scheduler.format_scheduler_text(report))
        except BrokenPipeError:
            return scheduler.scheduler_exit_code(report)

    return scheduler.scheduler_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
