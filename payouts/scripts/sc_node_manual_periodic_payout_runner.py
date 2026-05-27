#!/usr/bin/env python3
"""Manual-approved periodic SC-node payout runner (coordinates existing tooling only)."""

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

from payouts.collector.app import sc_node_manual_periodic_payout_runner as runner
from payouts.collector.app import sc_node_payout_cycle_readiness as cycle_readiness
from payouts.collector.app import sc_node_payout_production_preflight as production_preflight
from payouts.collector.app import sc_node_payout_status_summary as status_summary
from payouts.scripts import sc_node_payout_cycle_readiness as readiness_cli


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Manual-approved periodic payout runner (preview or execute-approved; "
            "no unattended scheduler)"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--payout-plan-id", type=int, required=True)
    common.add_argument("--production-preflight-id", type=int, required=True)
    common.add_argument(
        "--cycle-interval-minutes",
        type=int,
        default=None,
        help=(
            "Periodic cadence interval in minutes "
            f"(default env {runner.ENV_CYCLE_INTERVAL_MINUTES} or "
            f"{runner.DEFAULT_CYCLE_INTERVAL_MINUTES})"
        ),
    )
    common.add_argument(
        "--last-cycle-at",
        default=None,
        help="Optional ISO timestamp anchor for cadence when DB anchor unavailable",
    )
    common.add_argument(
        "--recommended-execution-mode",
        required=True,
        choices=["single", "chunked", "halt"],
        help="From latest production preflight preview utxo_chunking_policy",
    )
    common.add_argument(
        "--readiness-production-execution-id",
        type=int,
        default=None,
        help="Optional prior execution id to evaluate readiness gate",
    )
    common.add_argument(
        "--override-cadence-check",
        action="store_true",
        help="Allow execute when cadence interval has not elapsed (manual only)",
    )
    common.add_argument(
        "--override-cadence-reason",
        default=None,
        help="Required reason when --override-cadence-check is set",
    )

    subparsers.add_parser(
        "preview",
        parents=[common],
        help="Read-only cadence/readiness/preflight gate preview (no sends)",
    ).add_argument(
        "--idempotency-key",
        default=None,
        help="Optional idempotency key to assess duplicate-send risk in preview",
    )

    execute = subparsers.add_parser(
        "execute-approved",
        parents=[common],
        help="Execute after all gates pass and explicit operator approval",
    )
    execute.add_argument("--source-wallet-name", required=True)
    execute.add_argument(
        "--azc-bin",
        default=os.environ.get("AZC_BIN", "/usr/local/bin/azc-payout"),
        help="Send-capable wallet wrapper for delegated executor execute-real",
    )
    execute.add_argument("--idempotency-key", required=True)
    execute.add_argument(
        "--runner-approval-phrase",
        required=True,
        help=f"Must exactly match: {runner.RUNNER_APPROVAL_PHRASE}",
    )
    execute.add_argument(
        "--executor-confirm-phrase",
        required=True,
        help="Exact confirmation phrase from delegated executor preview output",
    )
    execute.add_argument(
        "--chunk-amount",
        default=None,
        help="Required when recommended_execution_mode=chunked",
    )
    execute.add_argument(
        "--dry-run-delegate",
        action="store_true",
        help="Validate gates and print delegated executor argv without subprocess send",
    )

    return parser.parse_args(argv)


def _database_url() -> str:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        raise SystemExit(1)
    return database_url


def _emit_json(payload: dict[str, object]) -> None:
    try:
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    except BrokenPipeError:
        raise SystemExit(0)


def _load_preflight(cur: psycopg.Cursor, production_preflight_id: int) -> dict[str, object] | None:
    cur.execute(
        production_preflight.build_production_preflight_details_sql(production_preflight_id)
    )
    row = cur.fetchone()
    return dict(row) if row is not None else None


def _load_readiness_context(
    cur: psycopg.Cursor,
    production_execution_id: int,
) -> tuple[dict[str, object], int, dict[str, object] | None] | None:
    return readiness_cli._load_context(cur, production_execution_id)


def _build_bundle(
    cur: psycopg.Cursor,
    *,
    payout_plan_id: int,
    production_preflight_id: int,
    cycle_interval_minutes: int,
    last_cycle_at: str | None,
    override_cadence_check: bool,
    override_cadence_reason: str | None,
    recommended_execution_mode: str,
    readiness_production_execution_id: int | None,
    idempotency_key: str | None = None,
    runner_approval_phrase: str | None = None,
    require_runner_approval: bool = False,
) -> dict[str, object]:
    now = datetime.now(timezone.utc)

    cur.execute(runner.build_last_confirmed_execution_sql())
    last_confirmed = cur.fetchone()

    cadence = runner.evaluate_cadence_eligibility(
        now=now,
        cycle_interval_minutes=cycle_interval_minutes,
        last_confirmed_execution=last_confirmed,
        last_cycle_at_override=runner.parse_optional_datetime(last_cycle_at),
        override_cadence_check=override_cadence_check,
        override_cadence_reason=override_cadence_reason,
    )

    plan_executions: list[dict[str, object]] = []
    if idempotency_key is not None:
        cur.execute(
            runner.build_plan_production_executions_sql(),
            {"payout_plan_id": payout_plan_id},
        )
        plan_executions = list(cur.fetchall())

    idempotency = (
        runner.evaluate_idempotency_state(
            payout_plan_id=payout_plan_id,
            idempotency_key=idempotency_key,
            plan_executions=plan_executions,
        )
        if idempotency_key is not None
        else None
    )

    preflight = _load_preflight(cur, production_preflight_id)

    readiness_verdict: str | None = None
    readiness_refusal: str | None = None
    readiness_report: dict[str, object] | None = None
    if readiness_production_execution_id is not None:
        loaded = _load_readiness_context(cur, readiness_production_execution_id)
        if loaded is None:
            readiness_verdict = cycle_readiness.VERDICT_HALT
            readiness_refusal = (
                f"readiness production execution not found: "
                f"{readiness_production_execution_id}"
            )
        else:
            summary, active_count, readiness_preflight = loaded
            readiness_report = cycle_readiness.evaluate_payout_cycle_readiness(
                summary=summary,
                active_chunked_reconciliation_count=active_count,
                preflight=readiness_preflight,
            )
            readiness_verdict, readiness_refusal = runner.evaluate_readiness_gate(
                summary=summary,
                active_chunked_reconciliation_count=active_count,
                preflight=readiness_preflight,
            )

    gates = runner.evaluate_runner_gates(
        cadence=cadence,
        idempotency=idempotency
        if idempotency is not None
        else runner.IdempotencyAssessment(
            idempotency_key="",
            existing_execution_id=None,
            existing_execution_status=None,
            plan_has_blocking_execution=False,
            blocking_execution_id=None,
            blocking_execution_status=None,
            may_execute=True,
            refusal_reason=None,
        ),
        preflight=preflight,
        recommended_execution_mode=recommended_execution_mode,
        runner_approval_phrase=runner_approval_phrase,
        require_runner_approval=require_runner_approval,
        readiness_verdict=readiness_verdict,
        readiness_refusal_reason=readiness_refusal,
    )

    payload: dict[str, object] = {
        "gates": runner.runner_gate_result_to_dict(gates),
        "preflight_id": production_preflight_id,
        "payout_plan_id": payout_plan_id,
    }
    if readiness_report is not None:
        payload["readiness_report"] = readiness_report
    if preflight is not None:
        payload["preflight_status"] = preflight.get("preflight_status")
        payload["preflight_execution_allowed"] = bool(preflight.get("execution_allowed"))
    return payload


def _cmd_preview(args: argparse.Namespace) -> int:
    interval = runner.parse_cycle_interval_minutes(cli_value=args.cycle_interval_minutes)
    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        with conn.cursor(row_factory=dict_row) as cur:
            payload = _build_bundle(
                cur,
                payout_plan_id=args.payout_plan_id,
                production_preflight_id=args.production_preflight_id,
                cycle_interval_minutes=interval,
                last_cycle_at=args.last_cycle_at,
                override_cadence_check=bool(args.override_cadence_check),
                override_cadence_reason=args.override_cadence_reason,
                recommended_execution_mode=args.recommended_execution_mode,
                readiness_production_execution_id=args.readiness_production_execution_id,
                idempotency_key=args.idempotency_key,
            )
    _emit_json({"command": "preview", **payload})
    return 0 if bool(payload["gates"]["allowed"]) else 2


def _delegate_argv(args: argparse.Namespace, mode: str) -> list[str]:
    if mode == runner.EXECUTOR_MODE_SINGLE:
        return runner.build_single_executor_delegate_argv(
            python_executable=sys.executable,
            repo_script_path=str(REPO_ROOT / runner.single_executor_script_relpath()),
            payout_plan_id=args.payout_plan_id,
            production_preflight_id=args.production_preflight_id,
            source_wallet_name=args.source_wallet_name,
            azc_bin=args.azc_bin,
            idempotency_key=args.idempotency_key,
            executor_confirm_phrase=args.executor_confirm_phrase,
        )
    if mode == runner.EXECUTOR_MODE_CHUNKED:
        if args.chunk_amount is None:
            print("--chunk-amount is required for chunked execution", file=sys.stderr)
            raise SystemExit(2)
        return runner.build_chunked_executor_delegate_argv(
            python_executable=sys.executable,
            repo_script_path=str(REPO_ROOT / runner.chunked_executor_script_relpath()),
            payout_plan_id=args.payout_plan_id,
            production_preflight_id=args.production_preflight_id,
            source_wallet_name=args.source_wallet_name,
            azc_bin=args.azc_bin,
            idempotency_key=args.idempotency_key,
            executor_confirm_phrase=args.executor_confirm_phrase,
            chunk_amount=args.chunk_amount,
        )
    print("recommended_execution_mode=halt cannot delegate execution", file=sys.stderr)
    raise SystemExit(2)


def _cmd_execute_approved(args: argparse.Namespace) -> int:
    interval = runner.parse_cycle_interval_minutes(cli_value=args.cycle_interval_minutes)
    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        with conn.cursor(row_factory=dict_row) as cur:
            payload = _build_bundle(
                cur,
                payout_plan_id=args.payout_plan_id,
                production_preflight_id=args.production_preflight_id,
                cycle_interval_minutes=interval,
                last_cycle_at=args.last_cycle_at,
                override_cadence_check=bool(args.override_cadence_check),
                override_cadence_reason=args.override_cadence_reason,
                recommended_execution_mode=args.recommended_execution_mode,
                readiness_production_execution_id=args.readiness_production_execution_id,
                idempotency_key=args.idempotency_key,
                runner_approval_phrase=args.runner_approval_phrase,
                require_runner_approval=True,
            )

    gates = payload["gates"]
    idempotency = gates["idempotency"]
    if idempotency.get("existing_execution_status") in {
        "sent",
        "confirmed",
    } and idempotency.get("existing_execution_id") is not None:
        _emit_json(
            {
                "command": "execute-approved",
                "executed": False,
                "idempotent_replay": True,
                "production_execution_id": idempotency["existing_execution_id"],
                **payload,
            }
        )
        return 0

    if not bool(gates.get("allowed")):
        _emit_json(
            {
                "command": "execute-approved",
                "executed": False,
                "idempotent_replay": False,
                **payload,
            }
        )
        return 2

    mode = str(gates.get("recommended_execution_mode"))
    delegate_argv = _delegate_argv(args, mode)
    if args.dry_run_delegate:
        _emit_json(
            {
                "command": "execute-approved",
                "executed": False,
                "dry_run_delegate": True,
                "delegate_argv": delegate_argv,
                **payload,
            }
        )
        return 0

    completed = subprocess.run(
        delegate_argv,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "delegated executor failed").strip()
        print(message, file=sys.stderr)
        _emit_json(
            {
                "command": "execute-approved",
                "executed": False,
                "delegate_returncode": completed.returncode,
                "delegate_stderr": completed.stderr,
                "delegate_stdout": completed.stdout,
                **payload,
            }
        )
        return completed.returncode

    try:
        delegate_payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        delegate_payload = {"raw_stdout": completed.stdout}
    _emit_json(
        {
            "command": "execute-approved",
            "executed": True,
            "idempotent_replay": False,
            "delegate_result": delegate_payload,
            **payload,
        }
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "preview":
        return _cmd_preview(args)
    if args.command == "execute-approved":
        return _cmd_execute_approved(args)
    print(f"unknown command: {args.command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
