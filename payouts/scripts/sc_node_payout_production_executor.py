#!/usr/bin/env python3
"""Production SC-node payout executor (execute-real uses sendtoaddress only)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from payouts.collector.app import payout_addresses
from payouts.collector.app import sc_node_payout_planner as planner
from payouts.collector.app import sc_node_payout_production_executor as executor


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Production payout executor (preview default; execute-real sends)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--payout-plan-id", type=int, required=True)
    common.add_argument("--production-preflight-id", type=int, required=True)
    common.add_argument("--source-wallet-name", required=True)
    common.add_argument(
        "--azc-bin",
        default=os.environ.get("AZC_BIN", "azc"),
        help="Wallet CLI binary (e.g. /tmp/azc wrapper to azcoin-cli)",
    )

    subparsers.add_parser(
        "preview",
        parents=[common],
        help="Preview production execution (getbalances only; no sends)",
    )

    execute_parser = subparsers.add_parser(
        "execute-real",
        parents=[common],
        help="Execute real sendtoaddress after all safety checks",
    )
    execute_parser.add_argument("--idempotency-key", required=True)
    execute_parser.add_argument("--confirm-phrase", required=True)
    execute_parser.add_argument(
        "--allow-multiple-rows",
        action="store_true",
        help="Allow execute-real with more than one payout row (v0 default refuses)",
    )
    execute_parser.add_argument("--notes", default=None)

    confirm_parser = subparsers.add_parser(
        "mark-confirmed",
        help="Mark production execution sent -> confirmed (gettransaction guard)",
    )
    confirm_parser.add_argument("--production-execution-id", type=int, required=True)
    confirm_parser.add_argument(
        "--confirm-chain-evidence",
        action="store_true",
        help="Required: verify tx confirmations via read-only gettransaction",
    )
    confirm_parser.add_argument(
        "--source-wallet-name",
        help="Source wallet for read-only gettransaction (required with --confirm-chain-evidence)",
    )
    confirm_parser.add_argument(
        "--azc-bin",
        default=os.environ.get("AZC_BIN", "/usr/local/bin/azc-payout-readonly"),
        help="Read-only wallet CLI for gettransaction",
    )
    confirm_parser.add_argument(
        "--min-confirmations",
        type=int,
        default=1,
        help="Minimum confirmations required from gettransaction (default 1)",
    )

    details_parser = subparsers.add_parser(
        "details",
        help="Show production execution and rows (no wallet RPC)",
    )
    details_parser.add_argument("--production-execution-id", type=int, required=True)

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


def _getbalances_argv(*, azc_bin: str, source_wallet_name: str) -> list[str]:
    executor.assert_no_forbidden_wallet_rpc_keywords_except_sendtoaddress(azc_bin)
    argv = [
        azc_bin,
        f"-rpcwallet={source_wallet_name}",
        "getbalances",
    ]
    for arg in argv:
        executor.assert_no_forbidden_wallet_rpc_keywords_except_sendtoaddress(arg)
    return argv


def _run_getbalances(*, azc_bin: str, source_wallet_name: str) -> dict[str, Any]:
    argv = _getbalances_argv(azc_bin=azc_bin, source_wallet_name=source_wallet_name)
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "getbalances failed").strip()
        print(message, file=sys.stderr)
        raise SystemExit(completed.returncode)
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        print(f"invalid JSON from getbalances: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not isinstance(parsed, dict):
        print("getbalances must return a JSON object", file=sys.stderr)
        raise SystemExit(1)
    return parsed


def _run_sendtoaddress(
    *,
    azc_bin: str,
    source_wallet_name: str,
    payout_address: str,
    payout_amount: Decimal,
) -> str:
    argv = executor.build_sendtoaddress_argv(
        azc_bin=azc_bin,
        source_wallet_name=source_wallet_name,
        payout_address=payout_address,
        payout_amount=payout_amount,
    )
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        message = (
            completed.stderr or completed.stdout or "sendtoaddress failed"
        ).strip()
        raise RuntimeError(message)
    txid = completed.stdout.strip()
    if not txid:
        raise RuntimeError("sendtoaddress returned empty txid")
    return txid


def _address_lookup(conn: psycopg.Connection) -> dict[str, list[dict[str, object]]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(payout_addresses.build_active_default_payout_addresses_sql())
        rows = cur.fetchall()
    lookup: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        sc_node_id = str(row["sc_node_id"])
        lookup.setdefault(sc_node_id, []).append(dict(row))
    return lookup


def _load_execution_bundle(
    conn: psycopg.Connection,
    *,
    payout_plan_id: int,
    production_preflight_id: int,
) -> tuple[
    dict[str, object] | None,
    list[dict[str, object]],
    dict[str, object] | None,
    list[dict[str, object]],
    dict[str, list[dict[str, object]]],
]:
    params = {
        "payout_plan_id": payout_plan_id,
        "production_preflight_id": production_preflight_id,
    }
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(executor.build_approved_payout_plan_for_execution_sql(payout_plan_id))
        plan = cur.fetchone()
        cur.execute(
            executor.build_approved_payout_plan_rows_for_execution_sql(payout_plan_id)
        )
        plan_rows = list(cur.fetchall())
        cur.execute(executor.build_passed_production_preflight_sql(), params)
        preflight = cur.fetchone()
        cur.execute(executor.build_production_preflight_rows_for_execution_sql(), params)
        preflight_rows = list(cur.fetchall())
    return plan, plan_rows, preflight, preflight_rows, _address_lookup(conn)


def _load_execution_details(
    conn: psycopg.Connection,
    production_execution_id: int,
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            executor.build_production_execution_details_sql(production_execution_id)
        )
        header = cur.fetchone()
        cur.execute(
            executor.build_production_execution_rows_sql(production_execution_id)
        )
        rows = list(cur.fetchall())
    return header, rows


def _insert_refused_execution(
    cur: psycopg.Cursor,
    *,
    args: argparse.Namespace,
    source_wallet: str,
    confirmation_phrase: str,
    idempotency_key: str,
    preview: executor.ProductionExecutionPreview,
    refusal_reason: str,
) -> int:
    cur.execute(
        executor.build_insert_production_execution_sql(),
        {
            "payout_plan_id": args.payout_plan_id,
            "production_preflight_id": args.production_preflight_id,
            "source_wallet_name": source_wallet,
            "status": executor.EXECUTION_STATUS_REFUSED,
            "planned_amount_total": preview.planned_amount_total,
            "trusted_balance_before": preview.wallet_balance.trusted,
            "immature_balance_before": preview.wallet_balance.immature,
            "reserve_amount": preview.reserve_amount,
            "spendable_after_reserve": preview.spendable_after_reserve,
            "execution_attempt_count": 0,
            "idempotency_key": idempotency_key,
            "confirmation_phrase": confirmation_phrase,
            "txid": None,
            "refusal_reason": refusal_reason,
            "notes": args.notes,
        },
    )
    inserted = cur.fetchone()
    if inserted is None:
        raise RuntimeError("failed to insert refused production execution")
    execution_id = int(inserted["id"])
    for row in preview.rows:
        cur.execute(
            executor.build_insert_production_execution_row_sql(),
            {
                "production_execution_id": execution_id,
                "payout_plan_row_id": row.payout_plan_row_id,
                "sc_node_id": row.sc_node_id,
                "payout_address": row.payout_address,
                "payout_amount": row.payout_amount,
                "row_status": executor.ROW_STATUS_REFUSED,
                "txid": None,
                "refusal_reason": refusal_reason,
            },
        )
    return execution_id


def _cmd_preview(args: argparse.Namespace) -> int:
    source_wallet = executor.normalize_source_wallet_name(args.source_wallet_name)
    balance_payload = _run_getbalances(
        azc_bin=args.azc_bin,
        source_wallet_name=source_wallet,
    )
    wallet_balance = executor.parse_wallet_balance_from_getbalances(balance_payload)

    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        plan, plan_rows, preflight, preflight_rows, address_lookup = _load_execution_bundle(
            conn,
            payout_plan_id=args.payout_plan_id,
            production_preflight_id=args.production_preflight_id,
        )
        preview = executor.build_production_execution_preview(
            payout_plan_id=args.payout_plan_id,
            production_preflight_id=args.production_preflight_id,
            source_wallet_name=source_wallet,
            plan=plan,
            plan_rows=plan_rows,
            preflight=preflight,
            preflight_rows=preflight_rows,
            wallet_balance=wallet_balance,
            address_lookup=address_lookup,
        )

    _emit_json(
        {
            "command": "preview",
            **executor.production_execution_preview_to_dict(preview),
        }
    )
    return 0


def _cmd_execute_real(args: argparse.Namespace) -> int:
    source_wallet = executor.normalize_source_wallet_name(args.source_wallet_name)
    idempotency_key = executor.normalize_idempotency_key(args.idempotency_key)
    confirmation_phrase = executor.normalize_confirmation_phrase(args.confirm_phrase)
    balance_payload = _run_getbalances(
        azc_bin=args.azc_bin,
        source_wallet_name=source_wallet,
    )
    wallet_balance = executor.parse_wallet_balance_from_getbalances(balance_payload)

    with psycopg.connect(_database_url()) as conn:
        plan, plan_rows, preflight, preflight_rows, address_lookup = _load_execution_bundle(
            conn,
            payout_plan_id=args.payout_plan_id,
            production_preflight_id=args.production_preflight_id,
        )
        preview = executor.build_production_execution_preview(
            payout_plan_id=args.payout_plan_id,
            production_preflight_id=args.production_preflight_id,
            source_wallet_name=source_wallet,
            plan=plan,
            plan_rows=plan_rows,
            preflight=preflight,
            preflight_rows=preflight_rows,
            wallet_balance=wallet_balance,
            address_lookup=address_lookup,
        )
        params = {
            "payout_plan_id": args.payout_plan_id,
            "idempotency_key": idempotency_key,
        }
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(executor.build_execution_by_plan_idempotency_sql(), params)
            existing = cur.fetchone()
            cur.execute(executor.build_existing_active_production_execution_sql(), params)
            active = cur.fetchone()

        refusal = executor.evaluate_execute_real_refusal(
            plan=plan,
            plan_rows=plan_rows,
            preflight=preflight,
            preflight_rows=preflight_rows,
            source_wallet_name=source_wallet,
            wallet_balance=wallet_balance,
            address_lookup=address_lookup,
            confirmation_phrase=confirmation_phrase,
            existing_by_key=existing,
            active_execution=active,
            idempotency_key=idempotency_key,
            allow_multiple_rows=bool(args.allow_multiple_rows),
        )

        if existing is not None:
            header, rows = _load_execution_details(conn, int(existing["id"]))
            _emit_json(
                {
                    "command": "execute-real",
                    "executed": str(header.get("status") if header else "") == executor.EXECUTION_STATUS_SENT,
                    "idempotent_replay": True,
                    "production_execution": executor.row_to_production_execution_dict(
                        header
                    ),
                    "rows": [
                        executor.row_to_production_execution_row_dict(row)
                        for row in rows
                    ],
                }
            )
            return 0

        if refusal:
            with conn.cursor(row_factory=dict_row) as cur:
                execution_id = _insert_refused_execution(
                    cur,
                    args=args,
                    source_wallet=source_wallet,
                    confirmation_phrase=confirmation_phrase,
                    idempotency_key=idempotency_key,
                    preview=preview,
                    refusal_reason=refusal,
                )
            conn.commit()
            header, rows = _load_execution_details(conn, execution_id)
            print(refusal, file=sys.stderr)
            _emit_json(
                {
                    "command": "execute-real",
                    "executed": False,
                    "refusal_reason": refusal,
                    "production_execution": executor.row_to_production_execution_dict(
                        header
                    ),
                    "rows": [
                        executor.row_to_production_execution_row_dict(row)
                        for row in rows
                    ],
                }
            )
            return 1

        assert plan is not None
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                executor.build_insert_production_execution_sql(),
                {
                    "payout_plan_id": args.payout_plan_id,
                    "production_preflight_id": args.production_preflight_id,
                    "source_wallet_name": source_wallet,
                    "status": executor.EXECUTION_STATUS_DRAFT,
                    "planned_amount_total": preview.planned_amount_total,
                    "trusted_balance_before": preview.wallet_balance.trusted,
                    "immature_balance_before": preview.wallet_balance.immature,
                    "reserve_amount": preview.reserve_amount,
                    "spendable_after_reserve": preview.spendable_after_reserve,
                    "execution_attempt_count": 0,
                    "idempotency_key": idempotency_key,
                    "confirmation_phrase": confirmation_phrase,
                    "txid": None,
                    "refusal_reason": None,
                    "notes": args.notes,
                },
            )
            inserted = cur.fetchone()
            if inserted is None:
                print("failed to insert production execution", file=sys.stderr)
                return 1
            execution_id = int(inserted["id"])
            execution_row_ids: list[int] = []
            for row in plan_rows:
                cur.execute(
                    executor.build_insert_production_execution_row_sql(),
                    {
                        "production_execution_id": execution_id,
                        "payout_plan_row_id": int(row["id"]),
                        "sc_node_id": str(row["sc_node_id"]),
                        "payout_address": str(row["payout_address"]),
                        "payout_amount": planner._to_decimal(row["payout_amount"]),
                        "row_status": executor.ROW_STATUS_DRAFT,
                        "txid": None,
                        "refusal_reason": None,
                    },
                )
                row_inserted = cur.fetchone()
                if row_inserted is not None:
                    execution_row_ids.append(int(row_inserted["id"]))

        try:
            if len(plan_rows) != 1:
                raise RuntimeError("v0 execute-real supports exactly one payout row")
            plan_row = plan_rows[0]
            txid = _run_sendtoaddress(
                azc_bin=args.azc_bin,
                source_wallet_name=source_wallet,
                payout_address=str(plan_row["payout_address"]),
                payout_amount=planner._to_decimal(plan_row["payout_amount"]),
            )
        except RuntimeError as exc:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    executor.build_mark_production_execution_refused_sql(),
                    {
                        "production_execution_id": execution_id,
                        "refusal_reason": str(exc),
                    },
                )
                for row_id in execution_row_ids:
                    cur.execute(
                        executor.build_mark_production_execution_row_refused_sql(),
                        {
                            "production_execution_row_id": row_id,
                            "refusal_reason": str(exc),
                        },
                    )
            conn.commit()
            header, rows = _load_execution_details(conn, execution_id)
            print(str(exc), file=sys.stderr)
            _emit_json(
                {
                    "command": "execute-real",
                    "executed": False,
                    "refusal_reason": str(exc),
                    "production_execution": executor.row_to_production_execution_dict(
                        header
                    ),
                    "rows": [
                        executor.row_to_production_execution_row_dict(row)
                        for row in rows
                    ],
                }
            )
            return 1

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                executor.build_mark_production_execution_sent_sql(),
                {"production_execution_id": execution_id, "txid": txid},
            )
            if cur.fetchone() is None:
                print("failed to mark production execution sent", file=sys.stderr)
                return 1
            for row_id in execution_row_ids:
                cur.execute(
                    executor.build_mark_production_execution_row_sent_sql(),
                    {
                        "production_execution_row_id": row_id,
                        "txid": txid,
                    },
                )
        conn.commit()

        header, rows = _load_execution_details(conn, execution_id)
        assert header is not None
        _emit_json(
            {
                "command": "execute-real",
                "executed": True,
                "idempotent_replay": False,
                "txid": txid,
                "production_execution": executor.row_to_production_execution_dict(header),
                "rows": [
                    executor.row_to_production_execution_row_dict(row) for row in rows
                ],
            }
        )
    return 0


def _run_gettransaction(
    *,
    azc_bin: str,
    source_wallet_name: str,
    txid: str,
) -> dict[str, Any]:
    argv = executor.build_mark_confirmed_gettransaction_argv(
        azc_bin=azc_bin,
        source_wallet_name=source_wallet_name,
        txid=txid,
    )
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "gettransaction failed").strip()
        raise RuntimeError(message)
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON from gettransaction: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("gettransaction must return a JSON object")
    return parsed


def _cmd_mark_confirmed(args: argparse.Namespace) -> int:
    with psycopg.connect(_database_url()) as conn:
        header, rows_before = _load_execution_details(conn, args.production_execution_id)
        refusal = executor.evaluate_mark_confirmed_refusal(header)
        if refusal and str(header.get("status") if header else "") != executor.EXECUTION_STATUS_CONFIRMED:
            print(refusal, file=sys.stderr)
            _emit_json(
                {
                    "command": "mark-confirmed",
                    "confirmed": False,
                    "refusal_reason": refusal,
                }
            )
            return 1

        if header is not None and str(header.get("status")) == executor.EXECUTION_STATUS_CONFIRMED:
            _emit_json(
                {
                    "command": "mark-confirmed",
                    "confirmed": True,
                    "idempotent_replay": True,
                    "production_execution": executor.row_to_production_execution_dict(
                        header
                    ),
                    "rows": [
                        executor.row_to_production_execution_row_dict(row)
                        for row in rows_before
                    ],
                }
            )
            return 0

        assert header is not None
        chain_prereq = executor.evaluate_mark_confirmed_chain_prereq_refusal(
            execution=header,
            confirm_chain_evidence=args.confirm_chain_evidence,
            source_wallet_name=args.source_wallet_name,
        )
        if chain_prereq:
            print(chain_prereq, file=sys.stderr)
            _emit_json(
                {
                    "command": "mark-confirmed",
                    "confirmed": False,
                    "refusal_reason": chain_prereq,
                }
            )
            return 1

        txid = str(header.get("txid") or "").strip()
        try:
            source_payload = _run_gettransaction(
                azc_bin=args.azc_bin,
                source_wallet_name=str(args.source_wallet_name),
                txid=txid,
            )
        except RuntimeError as exc:
            message = str(exc)
            print(message, file=sys.stderr)
            _emit_json(
                {
                    "command": "mark-confirmed",
                    "confirmed": False,
                    "refusal_reason": message,
                }
            )
            return 1

        confirmations = executor.parse_mark_confirmed_confirmations(source_payload, txid)
        chain_refusal = executor.evaluate_mark_confirmed_confirmations_refusal(
            confirmations=confirmations,
            min_confirmations=args.min_confirmations,
        )
        if chain_refusal:
            print(chain_refusal, file=sys.stderr)
            _emit_json(
                {
                    "command": "mark-confirmed",
                    "confirmed": False,
                    "refusal_reason": chain_refusal,
                    "confirmations": confirmations,
                }
            )
            return 1

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                executor.build_mark_production_execution_confirmed_sql(),
                {"production_execution_id": args.production_execution_id},
            )
            if cur.fetchone() is None:
                print("failed to confirm production execution", file=sys.stderr)
                return 1
            cur.execute(
                executor.build_mark_production_execution_rows_confirmed_sql(),
                {"production_execution_id": args.production_execution_id},
            )
        conn.commit()

        header, rows = _load_execution_details(conn, args.production_execution_id)
        assert header is not None
        _emit_json(
            {
                "command": "mark-confirmed",
                "confirmed": True,
                "idempotent_replay": False,
                "confirmations": confirmations,
                "production_execution": executor.row_to_production_execution_dict(
                    header
                ),
                "rows": [
                    executor.row_to_production_execution_row_dict(row) for row in rows
                ],
            }
        )
    return 0


def _cmd_details(args: argparse.Namespace) -> int:
    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        header, rows = _load_execution_details(conn, args.production_execution_id)
        if header is None:
            print(
                f"production execution not found: {args.production_execution_id}",
                file=sys.stderr,
            )
            return 1
        _emit_json(
            {
                "command": "details",
                "production_execution": executor.row_to_production_execution_dict(
                    header
                ),
                "rows": [
                    executor.row_to_production_execution_row_dict(row) for row in rows
                ],
            }
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "preview":
        return _cmd_preview(args)
    if args.command == "execute-real":
        return _cmd_execute_real(args)
    if args.command == "mark-confirmed":
        return _cmd_mark_confirmed(args)
    if args.command == "details":
        return _cmd_details(args)
    print(f"unknown command: {args.command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
