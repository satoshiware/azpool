#!/usr/bin/env python3
"""Fresh-cycle SC-node payout automation (baseline-gated; no historical backlog)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psycopg
from psycopg.rows import dict_row

from payouts.collector.app import sc_node_credit_ledger as credit_ledger
from payouts.collector.app import sc_node_fresh_cycle_automation as automation
from payouts.collector.app import sc_node_payout_plan_review as plan_review
from payouts.collector.app import sc_node_payout_planner as payout_planner
from payouts.collector.app import sc_node_payout_production_chunked_executor as chunked_executor
from payouts.collector.app import sc_node_payout_production_preflight as production_preflight
from payouts.scripts import sc_node_payout_production_preflight as preflight_cli


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fresh-cycle SC-node payout automation (baseline-gated; no backlog sends)"
        )
    )
    parser.add_argument(
        "--scheduler-env-path",
        default=automation.DEFAULT_SCHEDULER_ENV_PATH,
        help="Path to payout-scheduler.env target file",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--scan-rewards-first",
        action="store_true",
        help="Run support_wallet_reward_events scan --write before selection",
    )
    common.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    subparsers.add_parser(
        "preview",
        parents=[common],
        help="Preview fresh-cycle selection and payout path",
    )
    subparsers.add_parser(
        "write-target",
        parents=[common],
        help="Write credit/plan/preflight and report-only scheduler target",
    )
    subparsers.add_parser(
        "execute-live",
        parents=[common],
        help="Write artifacts and delegate execution through existing runner path",
    )
    subparsers.add_parser(
        "confirm-sent",
        parents=[common],
        help="Mark sent fresh-cycle executions confirmed (read-only chain evidence)",
    )
    return parser.parse_args(argv)


def _database_url() -> str:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        raise SystemExit(1)
    return database_url


def _emit_json(payload: dict[str, Any]) -> None:
    try:
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    except BrokenPipeError:
        raise SystemExit(0)


def _emit_safe_skip(reason: str, *, as_json: bool) -> int:
    message = automation.format_safe_skip_message(reason)
    if as_json:
        _emit_json({"safe_skip": True, "message": message})
    else:
        sys.stdout.write(message + "\n")
    return 0


def _maybe_scan_rewards(*, wallet_name: str, azc_bin: str) -> None:
    script = REPO_ROOT / "payouts/scripts/support_wallet_reward_events.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "scan",
            "--wallet",
            wallet_name,
            "--write",
            "--azc-bin",
            azc_bin,
        ],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "reward scan failed").strip()
        print(message, file=sys.stderr)
        raise SystemExit(completed.returncode)


def _load_selection(
    conn: psycopg.Connection,
    *,
    config: automation.FreshCycleConfig,
) -> automation.FreshCycleSelection | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            automation.build_latest_credit_run_coverage_end_sql(),
            {"wallet_name": config.wallet_name},
        )
        latest_row = cur.fetchone() or {}
        latest_end = latest_row.get("latest_coverage_end")
        cur.execute(
            automation.build_unlinked_mature_reward_events_sql(),
            {"wallet_name": config.wallet_name},
        )
        unlinked = list(cur.fetchall())
        exclude_boundary = False
        coverage_start = automation.compute_coverage_start(
            automation_baseline=config.automation_baseline,
            latest_credit_run_coverage_end=latest_end,
        )
        cur.execute(
            credit_ledger.build_prior_credit_run_coverage_end_match_sql(),
            {
                "wallet_name": config.wallet_name,
                "coverage_start": coverage_start,
            },
        )
        boundary_row = cur.fetchone() or {}
        exclude_boundary = bool(boundary_row.get("exclude_coverage_start_boundary"))
    return automation.build_fresh_cycle_selection(
        config=config,
        unlinked_events=unlinked,
        latest_credit_run_coverage_end=latest_end,
        exclude_coverage_start_boundary=exclude_boundary,
    )


def _load_credit_preview(
    conn: psycopg.Connection,
    *,
    config: automation.FreshCycleConfig,
    selection: automation.FreshCycleSelection,
) -> credit_ledger.CreditRunPreview:
    coverage = automation.build_credit_coverage(selection)
    params = {
        "wallet_name": config.wallet_name,
        "coverage_start": coverage.coverage_start,
        "coverage_end": coverage.coverage_end,
        "exclude_coverage_start_boundary": selection.exclude_coverage_start_boundary,
    }
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(credit_ledger.build_eligible_mature_rewards_sql(), params)
        reward_rows = cur.fetchall()
        cur.execute(credit_ledger.build_sc_node_work_share_sql(), params)
        sc_node_rows = cur.fetchall()
        cur.execute(credit_ledger.build_unmapped_work_sql(), params)
        unmapped_row = cur.fetchone()
    return credit_ledger.build_credit_run_preview(
        wallet_name=config.wallet_name,
        coverage=coverage,
        reward_rows=reward_rows,
        sc_node_rows=sc_node_rows,
        unmapped_row=unmapped_row,
    )


def _write_credit_run(
    conn: psycopg.Connection,
    *,
    config: automation.FreshCycleConfig,
    selection: automation.FreshCycleSelection,
    credit_preview: credit_ledger.CreditRunPreview,
) -> int:
    reward_event_ids = [
        int(row["reward_event_id"]) for row in selection.fresh_reward_events
    ]
    with conn.cursor(row_factory=dict_row) as cur:
        if reward_event_ids:
            cur.execute(
                credit_ledger.build_existing_reward_event_links_sql(),
                {"reward_event_ids": reward_event_ids},
            )
            existing_links = cur.fetchall()
            if existing_links:
                raise RuntimeError("fresh reward event became linked before write")
        cur.execute(
            credit_ledger.build_insert_credit_run_sql(),
            {
                "run_label": automation.build_credit_run_label(),
                "wallet_name": config.wallet_name,
                "maturity_status": credit_ledger.CREDIT_MATURITY_STATUS,
                "coverage_start": selection.coverage_start,
                "coverage_end": selection.coverage_end,
                "reward_event_count": credit_preview.reward_event_count,
                "reward_amount_total": credit_preview.reward_amount_total,
                "mapped_work_total": credit_preview.mapped_work_total,
                "unmapped_work_total": credit_preview.unmapped_work.work_delta_total,
                "status": "draft",
                "notes": "fresh-cycle-automation",
            },
        )
        run_row = cur.fetchone()
        if run_row is None:
            raise RuntimeError("failed to insert credit run")
        credit_run_id = int(run_row["id"])
        for credit in credit_preview.sc_node_credits:
            cur.execute(
                credit_ledger.build_insert_credit_sql(),
                {
                    "credit_run_id": credit_run_id,
                    "sc_node_id": credit.sc_node_id,
                    "reward_amount_total": credit_preview.reward_amount_total,
                    "work_delta_total": credit.work_delta_total,
                    "work_share": credit.work_share,
                    "credit_amount": credit.credit_amount,
                    "credit_status": "draft",
                },
            )
        for row in selection.fresh_reward_events:
            cur.execute(
                credit_ledger.build_insert_credit_run_event_sql(),
                {
                    "credit_run_id": credit_run_id,
                    "reward_event_id": int(row["reward_event_id"]),
                },
            )
    return credit_run_id


def _write_payout_plan(
    conn: psycopg.Connection,
    *,
    credit_run_id: int,
    config: automation.FreshCycleConfig,
    trusted_balance: Decimal,
) -> int:
    params = {"credit_run_id": credit_run_id}
    address_lookup: dict[str, list[dict[str, object]]] = {}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(payout_planner.build_credit_run_for_plan_sql(), params)
        credit_run = cur.fetchone()
        cur.execute(payout_planner.build_credits_for_plan_sql(), params)
        credits = cur.fetchall()
        cur.execute(payout_planner.build_existing_draft_plan_sql(), params)
        existing = cur.fetchone()
        for credit in credits:
            sc_node_id = str(credit["sc_node_id"])
            if sc_node_id in address_lookup:
                continue
            cur.execute(
                payout_planner.build_active_default_payout_addresses_sql(),
                {"sc_node_id": sc_node_id},
            )
            address_lookup[sc_node_id] = list(cur.fetchall())
        preview = payout_planner.build_payout_plan_preview(
            credit_run_id=credit_run_id,
            wallet_name=config.wallet_name,
            reserve_fraction=config.reserve_fraction,
            trusted_balance_snapshot=trusted_balance,
            credit_run=credit_run,
            credits=credits,
            address_lookup=address_lookup,
            existing_draft_plan_id=int(existing["id"]) if existing else None,
        )
        if not preview.plan_allowed:
            raise RuntimeError(preview.refusal_reason or "payout plan refused")
        cur.execute(
            payout_planner.build_insert_payout_plan_sql(),
            {
                "credit_run_id": preview.credit_run_id,
                "wallet_name": preview.wallet_name,
                "status": "draft",
                "reserve_fraction": preview.reserve_fraction,
                "trusted_balance_snapshot": preview.trusted_balance_snapshot,
                "reserve_amount": preview.reserve_amount,
                "max_spendable_amount": preview.max_spendable_amount,
                "planned_amount_total": preview.planned_amount_total,
                "row_count": preview.row_count,
                "notes": "fresh-cycle-automation",
                "payout_correction_id": preview.payout_correction_id,
            },
        )
        plan_row = cur.fetchone()
        if plan_row is None:
            raise RuntimeError("failed to insert payout plan")
        payout_plan_id = int(plan_row["id"])
        for row in preview.rows:
            cur.execute(
                payout_planner.build_insert_payout_plan_row_sql(),
                {
                    "payout_plan_id": payout_plan_id,
                    "credit_id": row.credit_id,
                    "sc_node_id": row.sc_node_id,
                    "payout_address": row.payout_address,
                    "gross_credit_amount": row.gross_credit_amount,
                    "correction_amount": row.correction_amount,
                    "payout_amount": row.payout_amount,
                    "status": "draft",
                },
            )
    return payout_plan_id


def _approve_plan(conn: psycopg.Connection, *, payout_plan_id: int, approved_by: str) -> None:
    confirmation = plan_review.build_approval_confirmation_phrase(payout_plan_id)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(plan_review.build_payout_plan_for_review_sql(payout_plan_id))
        plan = cur.fetchone()
        cur.execute(plan_review.build_payout_plan_rows_for_review_sql(payout_plan_id))
        plan_rows = list(cur.fetchall())
        address_lookup: dict[str, list[dict[str, object]]] = {}
        for row in plan_rows:
            sc_node_id = str(row["sc_node_id"])
            if sc_node_id in address_lookup:
                continue
            cur.execute(
                payout_planner.build_active_default_payout_addresses_sql(),
                {"sc_node_id": sc_node_id},
            )
            address_lookup[sc_node_id] = list(cur.fetchall())
        refusal = plan_review.evaluate_approve_refusal(
            plan=plan,
            plan_rows=plan_rows,
            address_lookup=address_lookup,
            confirmation=confirmation,
            payout_plan_id=payout_plan_id,
        )
        if refusal:
            raise RuntimeError(refusal)
        cur.execute(
            plan_review.build_update_approve_plan_sql(),
            {
                "payout_plan_id": payout_plan_id,
                "approved_by": approved_by,
                "approval_note": "fresh-cycle-automation",
                "approval_confirmation_hash": plan_review.hash_approval_confirmation(
                    confirmation
                ),
            },
        )
        if cur.fetchone() is None:
            raise RuntimeError("failed to approve payout plan")
        cur.execute(
            plan_review.build_update_approve_rows_sql(),
            {"payout_plan_id": payout_plan_id},
        )


def _record_preflight(
    conn: psycopg.Connection,
    *,
    payout_plan_id: int,
    config: automation.FreshCycleConfig,
    credit_run_id: int,
) -> tuple[int, production_preflight.ProductionPayoutPreflightPreview]:
    source_wallet = production_preflight.normalize_source_wallet_name(config.wallet_name)
    balance_payload = preflight_cli._run_getbalances(
        azc_bin=config.azc_bin,
        source_wallet_name=source_wallet,
    )
    wallet_balance = production_preflight.parse_wallet_balance_from_getbalances(
        balance_payload
    )
    utxo_snapshot = preflight_cli._run_listunspent(
        azc_bin=config.azc_bin,
        source_wallet_name=source_wallet,
    )
    idempotency_key = automation.build_preflight_idempotency_key(
        credit_run_id=credit_run_id
    )
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            production_preflight.build_production_preflight_by_idempotency_sql(),
            {"payout_plan_id": payout_plan_id, "idempotency_key": idempotency_key},
        )
        existing = cur.fetchone()
        if existing is not None:
            preflight_id = int(existing["id"])

        cur.execute(plan_review.build_payout_plan_for_review_sql(payout_plan_id))
        plan = cur.fetchone()
        cur.execute(plan_review.build_payout_plan_rows_for_review_sql(payout_plan_id))
        plan_rows = list(cur.fetchall())
        address_lookup: dict[str, list[dict[str, object]]] = {}
        for row in plan_rows:
            sc_node_id = str(row["sc_node_id"])
            if sc_node_id in address_lookup:
                continue
            cur.execute(
                payout_planner.build_active_default_payout_addresses_sql(),
                {"sc_node_id": sc_node_id},
            )
            address_lookup[sc_node_id] = list(cur.fetchall())
        preview = production_preflight.build_production_preflight_preview(
            payout_plan_id=payout_plan_id,
            source_wallet_name=source_wallet,
            plan=plan,
            plan_rows=plan_rows,
            wallet_balance=wallet_balance,
            address_lookup=address_lookup,
            operator_override=False,
            reserve_percent=config.reserve_fraction,
            reserve_amount=None,
            max_spend_percent=production_preflight.DEFAULT_MAX_SPEND_PERCENT,
            reserve_mode=production_preflight.RESERVE_MODE_PERCENT,
            utxo_snapshot=utxo_snapshot,
            target_single_tx_max_amount=config.target_single_tx_max_amount,
            fallback_chunk_amount=config.fallback_chunk_amount,
        )
        if existing is not None:
            return preflight_id, preview
        if not preview.execution_allowed:
            raise RuntimeError(preview.refusal_reason or "production preflight refused")
        cur.execute(
            production_preflight.build_insert_production_preflight_sql(),
            {
                "payout_plan_id": payout_plan_id,
                "source_wallet_name": source_wallet,
                "preflight_status": production_preflight.PREFLIGHT_STATUS_PASSED,
                "execution_allowed": True,
                "refusal_reason": None,
                "trusted_balance": preview.wallet_balance.trusted,
                "immature_balance": preview.wallet_balance.immature,
                "planned_amount_total": preview.planned_amount_total,
                "reserve_mode": preview.reserve_mode,
                "reserve_percent": preview.reserve_percent,
                "reserve_amount": preview.reserve_amount,
                "spendable_after_reserve": preview.spendable_after_reserve,
                "max_spend_percent": preview.max_spend_percent,
                "operator_override": preview.operator_override,
                "wallet_balance_source": production_preflight.WALLET_BALANCE_SOURCE_AZC_GETBALANCES,
                "idempotency_key": idempotency_key,
                "notes": "fresh-cycle-automation",
            },
        )
        header = cur.fetchone()
        if header is None:
            raise RuntimeError("failed to insert production preflight")
        preflight_id = int(header["id"])
        for row in preview.rows:
            cur.execute(
                production_preflight.build_insert_production_preflight_row_sql(),
                {
                    "production_preflight_id": preflight_id,
                    "payout_plan_row_id": row.payout_plan_row_id,
                    "sc_node_id": row.sc_node_id,
                    "payout_address": row.payout_address,
                    "payout_amount": row.payout_amount,
                    "row_status": row.row_status,
                    "refusal_reason": row.refusal_reason,
                },
            )
    return preflight_id, preview


def _write_scheduler_env(path: str, lines: list[str]) -> None:
    content = automation.render_scheduler_env_content(lines)
    target = Path(path)
    target.write_text(content, encoding="utf-8")
    try:
        os.chmod(target, 0o640)
    except OSError:
        pass


def _restore_safe_scheduler_env(path: str) -> None:
    _write_scheduler_env(path, automation.build_safe_skip_scheduler_env_lines())


def _cmd_preview(args: argparse.Namespace, config: automation.FreshCycleConfig) -> int:
    if args.scan_rewards_first:
        _maybe_scan_rewards(wallet_name=config.wallet_name, azc_bin=config.azc_bin)
    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        selection = _load_selection(conn, config=config)
        credit_preview = None
        execution_plan = None
        if selection is not None:
            credit_preview = _load_credit_preview(conn, config=config, selection=selection)
            if credit_preview.allocation_allowed:
                execution_plan = automation.FreshCycleExecutionPlan(
                    recommended_execution_mode=(
                        production_preflight.RECOMMENDED_EXECUTION_MODE_HALT
                    ),
                    chunk_amount=None,
                    expected_chunk_count=None,
                    executor_confirm_phrase=None,
                )
    if selection is None:
        return _emit_safe_skip("no fresh mature rewards after baseline", as_json=args.json)
    payload = automation.build_preview_summary(
        config=config,
        selection=selection,
        credit_preview=credit_preview,
        execution_plan=execution_plan,
    )
    if args.json:
        _emit_json(payload)
    else:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


def _write_artifacts(
    conn: psycopg.Connection,
    *,
    config: automation.FreshCycleConfig,
    selection: automation.FreshCycleSelection,
) -> tuple[int, int, int, production_preflight.ProductionPayoutPreflightPreview]:
    credit_preview = _load_credit_preview(conn, config=config, selection=selection)
    if not credit_preview.allocation_allowed:
        raise RuntimeError(credit_preview.refusal_reason or "credit allocation refused")
    credit_run_id = _write_credit_run(
        conn,
        config=config,
        selection=selection,
        credit_preview=credit_preview,
    )
    trusted_balance = preflight_cli._run_getbalances(
        azc_bin=config.azc_bin,
        source_wallet_name=config.wallet_name,
    )
    wallet_balance = production_preflight.parse_wallet_balance_from_getbalances(
        trusted_balance
    )
    payout_plan_id = _write_payout_plan(
        conn,
        credit_run_id=credit_run_id,
        config=config,
        trusted_balance=wallet_balance.trusted,
    )
    _approve_plan(conn, payout_plan_id=payout_plan_id, approved_by=config.approved_by)
    preflight_id, preflight_preview = _record_preflight(
        conn,
        payout_plan_id=payout_plan_id,
        config=config,
        credit_run_id=credit_run_id,
    )
    return credit_run_id, payout_plan_id, preflight_id, preflight_preview


def _cmd_write_target(args: argparse.Namespace, config: automation.FreshCycleConfig) -> int:
    if args.scan_rewards_first:
        _maybe_scan_rewards(wallet_name=config.wallet_name, azc_bin=config.azc_bin)
    with psycopg.connect(_database_url()) as conn:
        selection = _load_selection(conn, config=config)
        if selection is None:
            return _emit_safe_skip("no fresh mature rewards after baseline", as_json=args.json)
        credit_run_id, payout_plan_id, preflight_id, preflight_preview = _write_artifacts(
            conn,
            config=config,
            selection=selection,
        )
        execution_plan = automation.build_execution_plan(
            preflight_preview=preflight_preview,
            payout_plan_id=payout_plan_id,
            source_wallet_name=config.wallet_name,
        )
        conn.commit()
    _write_scheduler_env(
        args.scheduler_env_path,
        automation.build_scheduler_target_env_lines(
            payout_plan_id=payout_plan_id,
            production_preflight_id=preflight_id,
            recommended_execution_mode=execution_plan.recommended_execution_mode,
            source_wallet_name=config.wallet_name,
            chunk_amount=execution_plan.chunk_amount,
        ),
    )
    payload = automation.build_preview_summary(
        config=config,
        selection=selection,
        credit_preview=None,
        execution_plan=execution_plan,
        would_write=True,
        target_ids={
            "credit_run_id": credit_run_id,
            "payout_plan_id": payout_plan_id,
            "production_preflight_id": preflight_id,
        },
    )
    payload["scheduler_env_path"] = args.scheduler_env_path
    if args.json:
        _emit_json(payload)
    else:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


def _cmd_execute_live(args: argparse.Namespace, config: automation.FreshCycleConfig) -> int:
    refusal = automation.evaluate_execute_live_refusal(config)
    if refusal:
        print(refusal, file=sys.stderr)
        return 1
    if args.scan_rewards_first:
        _maybe_scan_rewards(wallet_name=config.wallet_name, azc_bin=config.azc_bin)
    try:
        with psycopg.connect(_database_url()) as conn:
            selection = _load_selection(conn, config=config)
            if selection is None:
                return _emit_safe_skip(
                    "no fresh mature rewards after baseline",
                    as_json=args.json,
                )
            credit_run_id, payout_plan_id, preflight_id, preflight_preview = _write_artifacts(
                conn,
                config=config,
                selection=selection,
            )
            execution_plan = automation.build_execution_plan(
                preflight_preview=preflight_preview,
                payout_plan_id=payout_plan_id,
                source_wallet_name=config.wallet_name,
            )
            if execution_plan.recommended_execution_mode == production_preflight.RECOMMENDED_EXECUTION_MODE_HALT:
                conn.rollback()
                print("execute-live refused: preflight recommends halt", file=sys.stderr)
                return 1
            if execution_plan.executor_confirm_phrase is None:
                conn.rollback()
                print("execute-live refused: missing executor confirmation phrase", file=sys.stderr)
                return 1
            idempotency_key = automation.build_execution_idempotency_key(
                credit_run_id=credit_run_id,
                payout_plan_id=payout_plan_id,
                production_preflight_id=preflight_id,
            )
            conn.commit()

        argv = automation.build_manual_runner_execute_argv(
            python_executable=sys.executable,
            repo_root=str(REPO_ROOT),
            payout_plan_id=payout_plan_id,
            production_preflight_id=preflight_id,
            recommended_execution_mode=execution_plan.recommended_execution_mode,
            idempotency_key=idempotency_key,
            source_wallet_name=config.wallet_name,
            azc_bin=config.azc_bin,
            runner_approval_phrase=config.runner_approval_phrase or automation.RUNNER_APPROVAL_PHRASE,
            executor_confirm_phrase=execution_plan.executor_confirm_phrase,
            chunk_amount=execution_plan.chunk_amount,
        )
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )
        stdout = automation.redact_secret_text(completed.stdout or "")
        stderr = automation.redact_secret_text(completed.stderr or "")
        if stdout:
            sys.stdout.write(stdout)
            if not stdout.endswith("\n"):
                sys.stdout.write("\n")
        if stderr:
            sys.stderr.write(stderr)
            if not stderr.endswith("\n"):
                sys.stderr.write("\n")
        execution_payload: dict[str, Any] = {
            "command": "execute-live",
            "credit_run_id": credit_run_id,
            "payout_plan_id": payout_plan_id,
            "production_preflight_id": preflight_id,
            "idempotency_key": idempotency_key,
            "delegate_returncode": completed.returncode,
        }
        try:
            runner_payload = json.loads(completed.stdout)
            execution_payload.update(
                {
                    "executed": runner_payload.get("executed"),
                    "execution_id": runner_payload.get("production_execution_id"),
                    "status": runner_payload.get("execution_status"),
                    "primary_txid": runner_payload.get("primary_txid"),
                }
            )
        except json.JSONDecodeError:
            pass
        if args.json:
            _emit_json(execution_payload)
        return 0 if completed.returncode == 0 else completed.returncode
    finally:
        _restore_safe_scheduler_env(args.scheduler_env_path)


def _cmd_confirm_sent(args: argparse.Namespace) -> int:
    database_url = _database_url()
    confirmed: list[dict[str, Any]] = []
    with psycopg.connect(database_url) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(automation.build_sent_fresh_cycle_executions_sql())
            rows = cur.fetchall()
    for row in rows:
        execution_id = int(row["id"])
        notes = str(row.get("notes") or "")
        if chunked_executor.is_chunked_execution_notes(notes):
            script = REPO_ROOT / "payouts/scripts/sc_node_payout_production_chunked_executor.py"
            argv = [
                sys.executable,
                str(script),
                "mark-confirmed",
                "--production-execution-id",
                str(execution_id),
            ]
        else:
            script = REPO_ROOT / "payouts/scripts/sc_node_payout_production_executor.py"
            argv = [
                sys.executable,
                str(script),
                "mark-confirmed",
                "--production-execution-id",
                str(execution_id),
                "--confirm-chain-evidence",
            ]
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )
        confirmed.append(
            {
                "production_execution_id": execution_id,
                "returncode": completed.returncode,
                "stdout": automation.redact_secret_text(completed.stdout or ""),
                "stderr": automation.redact_secret_text(completed.stderr or ""),
            }
        )
    payload = {"command": "confirm-sent", "results": confirmed}
    if args.json:
        _emit_json(payload)
    else:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0 if all(item["returncode"] == 0 for item in confirmed) else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    mode_map = {
        "preview": automation.MODE_PREVIEW,
        "write-target": automation.MODE_WRITE_TARGET,
        "execute-live": automation.MODE_EXECUTE_LIVE,
    }
    config = automation.load_config_from_env(
        mode_override=mode_map.get(args.command),
        scheduler_env_path=args.scheduler_env_path,
    )
    if args.command == "preview":
        return _cmd_preview(args, config)
    if args.command == "write-target":
        return _cmd_write_target(args, config)
    if args.command == "execute-live":
        return _cmd_execute_live(args, config)
    if args.command == "confirm-sent":
        return _cmd_confirm_sent(args)
    print(f"unknown command: {args.command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
