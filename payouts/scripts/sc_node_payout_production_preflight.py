#!/usr/bin/env python3
"""Production SC-node payout preflight (read-only getbalances; no sends)."""

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

from payouts.collector.app import sc_node_payout_production_preflight as production
from payouts.collector.app.sc_node_payout_planner import (
    build_active_default_payout_addresses_sql,
    parse_decimal_amount,
    parse_reserve_fraction,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Production payout preflight (getbalances only; no sends)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--payout-plan-id", type=int, required=True)
    common.add_argument("--source-wallet-name", required=True)
    common.add_argument(
        "--azc-bin",
        default=os.environ.get("AZC_BIN", "azc"),
        help="Wallet CLI for read-only getbalances (e.g. /tmp/azc on support node)",
    )
    common.add_argument(
        "--reserve-percent",
        default=None,
        help="Reserve fraction of trusted balance (default 0.5)",
    )
    common.add_argument(
        "--reserve-amount",
        default=None,
        help="Fixed reserve amount (reserve_mode=amount)",
    )
    common.add_argument(
        "--max-spend-percent",
        default="0.5",
        help="Max spend fraction of trusted balance (default 0.5)",
    )
    common.add_argument(
        "--override-reserve",
        action="store_true",
        help="Allow spend above default reserve (still capped at trusted balance)",
    )
    common.add_argument("--notes", default=None)

    subparsers.add_parser(
        "preview",
        parents=[common],
        help="Preview production preflight (no DB writes)",
    )

    record_parser = subparsers.add_parser(
        "record",
        parents=[common],
        help="Record production preflight audit rows",
    )
    record_parser.add_argument("--idempotency-key", required=True)

    details_parser = subparsers.add_parser("details", help="Show recorded preflight")
    details_parser.add_argument("--production-preflight-id", type=int, required=True)

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


def _reserve_options(args: argparse.Namespace) -> dict[str, object]:
    reserve_percent = (
        parse_reserve_fraction(args.reserve_percent)
        if args.reserve_percent is not None
        else production.DEFAULT_RESERVE_PERCENT
    )
    reserve_amount = (
        parse_decimal_amount(args.reserve_amount, field_name="reserve_amount")
        if args.reserve_amount is not None
        else None
    )
    max_spend_percent = parse_reserve_fraction(args.max_spend_percent)
    reserve_mode = (
        production.RESERVE_MODE_AMOUNT
        if reserve_amount is not None
        else production.RESERVE_MODE_PERCENT
    )
    return {
        "reserve_percent": reserve_percent,
        "reserve_amount": reserve_amount,
        "max_spend_percent": max_spend_percent,
        "reserve_mode": reserve_mode,
        "operator_override": bool(args.override_reserve),
    }


def _getbalances_argv(*, azc_bin: str, source_wallet_name: str) -> list[str]:
    production.assert_no_wallet_send_keywords(azc_bin)
    argv = [
        azc_bin,
        f"-rpcwallet={source_wallet_name}",
        "getbalances",
    ]
    for arg in argv:
        production.assert_no_wallet_send_keywords(arg)
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


def _address_lookup(conn: psycopg.Connection) -> dict[str, list[dict[str, object]]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(build_active_default_payout_addresses_sql())
        rows = cur.fetchall()
    lookup: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        sc_node_id = str(row["sc_node_id"])
        lookup.setdefault(sc_node_id, []).append(dict(row))
    return lookup


def _load_plan_bundle(
    conn: psycopg.Connection,
    payout_plan_id: int,
) -> tuple[dict[str, object] | None, list[dict[str, object]], dict[str, list[dict[str, object]]]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(production.build_approved_payout_plan_sql(payout_plan_id))
        plan = cur.fetchone()
        cur.execute(production.build_approved_payout_plan_rows_sql(payout_plan_id))
        plan_rows = list(cur.fetchall())
    return plan, plan_rows, _address_lookup(conn)


def _build_preview_from_args(
    args: argparse.Namespace,
    *,
    plan: dict[str, object] | None,
    plan_rows: list[dict[str, object]],
    wallet_balance: production.WalletBalance,
    address_lookup: dict[str, list[dict[str, object]]],
) -> production.ProductionPayoutPreflightPreview:
    opts = _reserve_options(args)
    return production.build_production_preflight_preview(
        payout_plan_id=args.payout_plan_id,
        source_wallet_name=production.normalize_source_wallet_name(args.source_wallet_name),
        plan=plan,
        plan_rows=plan_rows,
        wallet_balance=wallet_balance,
        address_lookup=address_lookup,
        operator_override=bool(opts["operator_override"]),
        reserve_percent=opts["reserve_percent"],  # type: ignore[arg-type]
        reserve_amount=opts["reserve_amount"],  # type: ignore[arg-type]
        max_spend_percent=opts["max_spend_percent"],  # type: ignore[arg-type]
        reserve_mode=str(opts["reserve_mode"]),
    )


def _cmd_preview(args: argparse.Namespace) -> int:
    source_wallet = production.normalize_source_wallet_name(args.source_wallet_name)
    balance_payload = _run_getbalances(
        azc_bin=args.azc_bin,
        source_wallet_name=source_wallet,
    )
    wallet_balance = production.parse_wallet_balance_from_getbalances(balance_payload)

    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        plan, plan_rows, address_lookup = _load_plan_bundle(conn, args.payout_plan_id)
        preview = _build_preview_from_args(
            args,
            plan=plan,
            plan_rows=plan_rows,
            wallet_balance=wallet_balance,
            address_lookup=address_lookup,
        )

    _emit_json(
        {
            "command": "preview",
            **production.production_preflight_preview_to_dict(preview),
        }
    )
    return 0


def _load_preflight_bundle(
    conn: psycopg.Connection,
    production_preflight_id: int,
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            production.build_production_preflight_details_sql(production_preflight_id)
        )
        header = cur.fetchone()
        cur.execute(
            production.build_production_preflight_rows_sql(production_preflight_id)
        )
        rows = list(cur.fetchall())
    return header, rows


def _cmd_record(args: argparse.Namespace) -> int:
    source_wallet = production.normalize_source_wallet_name(args.source_wallet_name)
    idempotency_key = production.normalize_idempotency_key(args.idempotency_key)
    balance_payload = _run_getbalances(
        azc_bin=args.azc_bin,
        source_wallet_name=source_wallet,
    )
    wallet_balance = production.parse_wallet_balance_from_getbalances(balance_payload)
    opts = _reserve_options(args)

    with psycopg.connect(_database_url()) as conn:
        params = {
            "payout_plan_id": args.payout_plan_id,
            "idempotency_key": idempotency_key,
        }
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(production.build_production_preflight_by_idempotency_sql(), params)
            existing = cur.fetchone()

        if existing is not None:
            header, rows = _load_preflight_bundle(conn, int(existing["id"]))
            _emit_json(
                {
                    "command": "record",
                    "recorded": False,
                    "idempotent_replay": True,
                    "production_preflight": production.row_to_production_preflight_dict(
                        header
                    ),
                    "rows": [
                        production.row_to_production_preflight_row_dict(row)
                        for row in rows
                    ],
                }
            )
            return 0

        plan, plan_rows, address_lookup = _load_plan_bundle(conn, args.payout_plan_id)
        preview = _build_preview_from_args(
            args,
            plan=plan,
            plan_rows=plan_rows,
            wallet_balance=wallet_balance,
            address_lookup=address_lookup,
        )
        preflight_status = (
            production.PREFLIGHT_STATUS_PASSED
            if preview.execution_allowed
            else production.PREFLIGHT_STATUS_REFUSED
        )

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                production.build_insert_production_preflight_sql(),
                {
                    "payout_plan_id": args.payout_plan_id,
                    "source_wallet_name": source_wallet,
                    "preflight_status": preflight_status,
                    "execution_allowed": preview.execution_allowed,
                    "refusal_reason": preview.refusal_reason,
                    "trusted_balance": wallet_balance.trusted,
                    "immature_balance": wallet_balance.immature,
                    "planned_amount_total": preview.planned_amount_total,
                    "reserve_mode": preview.reserve_mode,
                    "reserve_percent": preview.reserve_percent,
                    "reserve_amount": preview.reserve_amount,
                    "spendable_after_reserve": preview.spendable_after_reserve,
                    "max_spend_percent": preview.max_spend_percent,
                    "operator_override": preview.operator_override,
                    "wallet_balance_source": production.WALLET_BALANCE_SOURCE_AZC_GETBALANCES,
                    "idempotency_key": idempotency_key,
                    "notes": args.notes,
                },
            )
            inserted = cur.fetchone()
            if inserted is None:
                print("failed to insert production preflight", file=sys.stderr)
                return 1
            preflight_id = int(inserted["id"])

            for row in preview.rows:
                cur.execute(
                    production.build_insert_production_preflight_row_sql(),
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
        conn.commit()

        header, rows = _load_preflight_bundle(conn, preflight_id)
        assert header is not None
        _emit_json(
            {
                "command": "record",
                "recorded": True,
                "idempotent_replay": False,
                "production_preflight": production.row_to_production_preflight_dict(
                    header
                ),
                "rows": [
                    production.row_to_production_preflight_row_dict(row) for row in rows
                ],
            }
        )
    return 0


def _cmd_details(args: argparse.Namespace) -> int:
    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        header, rows = _load_preflight_bundle(conn, args.production_preflight_id)
        if header is None:
            print(
                f"production preflight not found: {args.production_preflight_id}",
                file=sys.stderr,
            )
            return 1
        _emit_json(
            {
                "command": "details",
                "production_preflight": production.row_to_production_preflight_dict(
                    header
                ),
                "rows": [
                    production.row_to_production_preflight_row_dict(row) for row in rows
                ],
            }
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "preview":
        return _cmd_preview(args)
    if args.command == "record":
        return _cmd_record(args)
    if args.command == "details":
        return _cmd_details(args)
    print(f"unknown command: {args.command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
