#!/usr/bin/env python3
"""SC-node payout fake/regtest test executor (no wallet RPC, no real sends)."""

from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg
from psycopg.rows import dict_row

from payouts.collector.app import sc_node_payout_test_executor as executor


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fake/regtest payout test execution harness (no azc, no sends)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--payout-plan-id", type=int, required=True)

    preview_parser = subparsers.add_parser(
        "preview",
        parents=[common],
        help="Preview fake_regtest execution (no writes)",
    )
    preview_parser.add_argument(
        "--mode",
        default=executor.EXECUTION_MODE_FAKE_REGTEST,
        help="Execution mode (fake_regtest or regtest)",
    )
    preview_parser.add_argument(
        "--test-wallet-name",
        default="fake-regtest-wallet",
        help="Test-only wallet label for preview output",
    )

    execute_parser = subparsers.add_parser(
        "execute-fake",
        parents=[common],
        help="Record fake_regtest execution (in-memory/fake txid only)",
    )
    execute_parser.add_argument(
        "--mode",
        default=executor.EXECUTION_MODE_FAKE_REGTEST,
        help="Must be fake_regtest",
    )
    execute_parser.add_argument(
        "--test-wallet-name",
        required=True,
        help="Test-only wallet name (fake- prefix or regtest)",
    )
    execute_parser.add_argument("--idempotency-key", required=True)
    execute_parser.add_argument("--notes", default=None)

    confirm_parser = subparsers.add_parser(
        "mark-confirmed",
        help="Mark fake execution sent -> confirmed",
    )
    confirm_parser.add_argument("--test-execution-id", type=int, required=True)

    details_parser = subparsers.add_parser(
        "details",
        help="Show test execution and rows",
    )
    details_parser.add_argument("--test-execution-id", type=int, required=True)

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


def _load_plan_bundle(
    conn: psycopg.Connection,
    payout_plan_id: int,
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(executor.build_payout_plan_for_test_sql(payout_plan_id))
        plan = cur.fetchone()
        cur.execute(executor.build_payout_plan_rows_for_test_sql(payout_plan_id))
        rows = list(cur.fetchall())
    return plan, rows


def _load_execution_bundle(
    conn: psycopg.Connection,
    test_execution_id: int,
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(executor.build_test_execution_details_sql(test_execution_id))
        header = cur.fetchone()
        cur.execute(executor.build_test_execution_rows_sql(test_execution_id))
        rows = list(cur.fetchall())
    return header, rows


def _cmd_preview(args: argparse.Namespace) -> int:
    mode = executor.normalize_execution_mode(args.mode)
    test_wallet = executor.normalize_test_wallet_name(args.test_wallet_name)

    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        plan, plan_rows = _load_plan_bundle(conn, args.payout_plan_id)
        preview = executor.build_test_execution_preview(
            payout_plan_id=args.payout_plan_id,
            mode=mode,
            test_wallet_name=test_wallet,
            plan=plan,
            plan_rows=plan_rows,
        )

    _emit_json({"command": "preview", **executor.test_execution_preview_to_dict(preview)})
    return 0


def _cmd_execute_fake(args: argparse.Namespace) -> int:
    mode = executor.normalize_execution_mode(args.mode)
    test_wallet = executor.normalize_test_wallet_name(args.test_wallet_name)
    idempotency_key = executor.normalize_idempotency_key(args.idempotency_key)

    with psycopg.connect(_database_url()) as conn:
        plan, plan_rows = _load_plan_bundle(conn, args.payout_plan_id)
        params = {
            "payout_plan_id": args.payout_plan_id,
            "idempotency_key": idempotency_key,
        }
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(executor.build_execution_by_plan_idempotency_sql(), params)
            existing = cur.fetchone()
            cur.execute(executor.build_active_execution_for_plan_sql(), params)
            active = cur.fetchone()

        refusal = executor.evaluate_execute_fake_refusal(
            plan=plan,
            plan_rows=plan_rows,
            mode=mode,
            test_wallet_name=test_wallet,
            existing_by_key=existing,
            active_execution=active,
            idempotency_key=idempotency_key,
        )
        if refusal:
            print(refusal, file=sys.stderr)
            _emit_json(
                {
                    "command": "execute-fake",
                    "executed": False,
                    "refusal_reason": refusal,
                }
            )
            return 1

        if existing is not None:
            header, rows = _load_execution_bundle(conn, int(existing["id"]))
            _emit_json(
                {
                    "command": "execute-fake",
                    "executed": False,
                    "idempotent_replay": True,
                    "test_execution": executor.row_to_test_execution_dict(header),
                    "rows": [executor.row_to_test_execution_row_dict(r) for r in rows],
                }
            )
            return 0

        assert plan is not None
        row_ids = [int(row["id"]) for row in plan_rows]
        fake_txid = executor.generate_fake_txid(
            payout_plan_id=args.payout_plan_id,
            idempotency_key=idempotency_key,
            payout_plan_row_ids=row_ids,
        )
        planned_total = executor.planner._to_decimal(plan.get("planned_amount_total"))

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                executor.build_insert_test_execution_sql(),
                {
                    "payout_plan_id": args.payout_plan_id,
                    "mode": mode,
                    "status": executor.EXECUTION_STATUS_SENT,
                    "planned_amount_total": planned_total,
                    "test_wallet_name": test_wallet,
                    "txid": fake_txid,
                    "execution_attempt_count": 1,
                    "idempotency_key": idempotency_key,
                    "notes": args.notes,
                },
            )
            inserted = cur.fetchone()
            if inserted is None:
                print("failed to insert test execution", file=sys.stderr)
                return 1
            test_execution_id = int(inserted["id"])

            for row in plan_rows:
                cur.execute(
                    executor.build_insert_test_execution_row_sql(),
                    {
                        "test_execution_id": test_execution_id,
                        "payout_plan_row_id": int(row["id"]),
                        "sc_node_id": str(row["sc_node_id"]),
                        "payout_address": str(row["payout_address"]),
                        "payout_amount": executor.planner._to_decimal(row["payout_amount"]),
                        "row_status": executor.ROW_STATUS_SENT,
                        "txid": fake_txid,
                    },
                )
        conn.commit()

        header, rows = _load_execution_bundle(conn, test_execution_id)
        assert header is not None
        _emit_json(
            {
                "command": "execute-fake",
                "executed": True,
                "idempotent_replay": False,
                "test_execution": executor.row_to_test_execution_dict(header),
                "rows": [executor.row_to_test_execution_row_dict(r) for r in rows],
            }
        )
    return 0


def _cmd_mark_confirmed(args: argparse.Namespace) -> int:
    with psycopg.connect(_database_url()) as conn:
        header, rows_before = _load_execution_bundle(conn, args.test_execution_id)
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
                    "test_execution": executor.row_to_test_execution_dict(header),
                    "rows": [
                        executor.row_to_test_execution_row_dict(r) for r in rows_before
                    ],
                }
            )
            return 0

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                executor.build_update_execution_confirmed_sql(),
                {"test_execution_id": args.test_execution_id},
            )
            if cur.fetchone() is None:
                print("failed to confirm test execution", file=sys.stderr)
                return 1
            cur.execute(
                executor.build_update_execution_rows_confirmed_sql(),
                {"test_execution_id": args.test_execution_id},
            )
        conn.commit()

        header, rows = _load_execution_bundle(conn, args.test_execution_id)
        assert header is not None
        _emit_json(
            {
                "command": "mark-confirmed",
                "confirmed": True,
                "idempotent_replay": False,
                "test_execution": executor.row_to_test_execution_dict(header),
                "rows": [executor.row_to_test_execution_row_dict(r) for r in rows],
            }
        )
    return 0


def _cmd_details(args: argparse.Namespace) -> int:
    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        header, rows = _load_execution_bundle(conn, args.test_execution_id)
        if header is None:
            print(f"test execution not found: {args.test_execution_id}", file=sys.stderr)
            return 1
        _emit_json(
            {
                "command": "details",
                "test_execution": executor.row_to_test_execution_dict(header),
                "rows": [executor.row_to_test_execution_row_dict(r) for r in rows],
            }
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "preview":
        return _cmd_preview(args)
    if args.command == "execute-fake":
        return _cmd_execute_fake(args)
    if args.command == "mark-confirmed":
        return _cmd_mark_confirmed(args)
    if args.command == "details":
        return _cmd_details(args)
    print(f"unknown command: {args.command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
