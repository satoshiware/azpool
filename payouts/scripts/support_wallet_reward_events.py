#!/usr/bin/env python3
"""Read-only support-wallet reward listener (listtransactions observe + optional DB upsert)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any

import psycopg
from psycopg.rows import dict_row

from payouts.collector.app import reward_events


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Observe support-wallet reward events via read-only azc listtransactions"
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    print_parser = subparsers.add_parser("print", help="Read reward events from Postgres")
    print_parser.add_argument(
        "--include-raw",
        action="store_true",
        help="Include raw_wallet_event in JSON output",
    )
    print_parser.add_argument(
        "--maturity-status",
        default=None,
        help="Filter by maturity_status",
    )

    scan_parser = subparsers.add_parser(
        "scan",
        help="Scan wallet via azc listtransactions (dry-run unless --write)",
    )
    scan_parser.add_argument("--wallet", required=True, help="Wallet name for -rpcwallet")
    scan_parser.add_argument("--count", type=int, default=100, help="listtransactions count")
    scan_parser.add_argument(
        "--maturity-confirmations",
        type=int,
        default=reward_events.DEFAULT_MATURITY_CONFIRMATIONS,
        help="Confirmations required for mature generate rewards",
    )
    scan_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry-run (default unless --write)",
    )
    scan_parser.add_argument(
        "--write",
        action="store_true",
        help="Upsert normalized events into Postgres (default: dry-run only)",
    )
    scan_parser.add_argument(
        "--azc-bin",
        default=os.environ.get("AZC_BIN", "azc"),
        help="azc executable (read-only listtransactions only)",
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


def _listtransactions_argv(*, azc_bin: str, wallet: str, count: int) -> list[str]:
    reward_events.assert_no_wallet_send_keywords(azc_bin)
    argv = [
        azc_bin,
        f"-rpcwallet={wallet}",
        "listtransactions",
        "*",
        str(count),
        "0",
    ]
    for arg in argv:
        reward_events.assert_no_wallet_send_keywords(arg)
    return argv


def _run_listtransactions(*, azc_bin: str, wallet: str, count: int) -> list[dict[str, Any]]:
    argv = _listtransactions_argv(azc_bin=azc_bin, wallet=wallet, count=count)
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "azc listtransactions failed").strip()
        print(message, file=sys.stderr)
        raise SystemExit(completed.returncode)
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        print(f"invalid JSON from azc: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not isinstance(parsed, list):
        print("azc listtransactions must return a JSON array", file=sys.stderr)
        raise SystemExit(1)
    return [row for row in parsed if isinstance(row, dict)]


def _normalize_scan_rows(
    rows: list[dict[str, Any]],
    *,
    wallet: str,
    maturity_confirmations: int,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        event = reward_events.wallet_event_to_reward_event(
            row,
            wallet_name=wallet,
            maturity_confirmations=maturity_confirmations,
        )
        if event is None:
            continue
        normalized.append(reward_events.reward_event_to_dict(event, include_raw=False))
    return normalized


def _cmd_print(args: argparse.Namespace) -> int:
    database_url = _database_url()
    sql = reward_events.build_reward_events_sql(
        include_raw=args.include_raw,
        maturity_status=args.maturity_status,
    )
    with psycopg.connect(database_url) as conn:
        conn.set_read_only(True)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql)
            rows = [
                reward_events.row_to_reward_event_dict(
                    row,
                    include_raw=args.include_raw,
                )
                for row in cur.fetchall()
            ]
    _emit_json({"mode": "print", "rows": rows})
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    if args.write and args.dry_run:
        print("cannot use --write and --dry-run together", file=sys.stderr)
        return 1

    wallet_rows = _run_listtransactions(
        azc_bin=args.azc_bin,
        wallet=args.wallet,
        count=args.count,
    )
    normalized = _normalize_scan_rows(
        wallet_rows,
        wallet=args.wallet,
        maturity_confirmations=args.maturity_confirmations,
    )

    if not args.write:
        _emit_json(
            {
                "mode": "scan",
                "dry_run": True,
                "wallet": args.wallet,
                "count": args.count,
                "maturity_confirmations": args.maturity_confirmations,
                "events": normalized,
            }
        )
        return 0

    database_url = _database_url()
    upsert_sql = reward_events.build_upsert_reward_event_sql()
    written = 0
    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            for row in wallet_rows:
                event = reward_events.wallet_event_to_reward_event(
                    row,
                    wallet_name=args.wallet,
                    maturity_confirmations=args.maturity_confirmations,
                )
                if event is None:
                    continue
                cur.execute(
                    upsert_sql,
                    reward_events.reward_event_to_upsert_params(event),
                )
                written += 1
        conn.commit()

    _emit_json(
        {
            "mode": "scan",
            "dry_run": False,
            "wallet": args.wallet,
            "written": written,
            "events": normalized,
        }
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.mode == "print":
        return _cmd_print(args)
    if args.mode == "scan":
        return _cmd_scan(args)
    print(f"unknown mode: {args.mode}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
