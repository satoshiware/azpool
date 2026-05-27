#!/usr/bin/env python3
"""SC-node payout correction ledger (draft offsets only; no wallet RPC, no sends)."""

from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg
from psycopg.rows import dict_row

from payouts.collector.app import sc_node_payout_correction as correction


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SC-node payout correction ledger (no wallet RPC, no sends)"
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    create_parser = subparsers.add_parser(
        "create-draft",
        help="Insert a draft payout correction offset",
    )
    create_parser.add_argument("--sc-node-id", required=True)
    create_parser.add_argument("--wallet", required=True)
    create_parser.add_argument("--amount", required=True, help="Positive offset amount")
    create_parser.add_argument("--reason-code", required=True)
    create_parser.add_argument("--notes", default=None)
    create_parser.add_argument("--related-credit-run-id", type=int, default=None)
    create_parser.add_argument("--related-reward-event-id", type=int, default=None)
    create_parser.add_argument("--related-txid", default=None)
    create_parser.add_argument("--created-by", default=None)

    subparsers.add_parser("list", help="List payout corrections")

    details_parser = subparsers.add_parser("details", help="Show one payout correction")
    details_parser.add_argument("--correction-id", type=int, required=True)

    cancel_parser = subparsers.add_parser("cancel", help="Cancel a draft payout correction")
    cancel_parser.add_argument("--correction-id", type=int, required=True)

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


def _load_correction(
    conn: psycopg.Connection,
    correction_id: int,
) -> dict[str, object] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(correction.build_correction_details_sql(correction_id))
        row = cur.fetchone()
    return dict(row) if row is not None else None


def _cmd_create_draft(args: argparse.Namespace) -> int:
    wallet_name = correction.normalize_wallet_name(args.wallet)
    amount = correction.parse_decimal_amount(args.amount, field_name="amount")
    with psycopg.connect(_database_url()) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                correction.build_insert_correction_sql(),
                {
                    "sc_node_id": args.sc_node_id,
                    "wallet_name": wallet_name,
                    "amount": amount,
                    "direction": correction.CORRECTION_DIRECTION_OFFSET_DEBIT,
                    "reason_code": args.reason_code,
                    "notes": args.notes,
                    "related_credit_run_id": args.related_credit_run_id,
                    "related_reward_event_id": args.related_reward_event_id,
                    "related_txid": args.related_txid,
                    "status": correction.CORRECTION_STATUS_DRAFT,
                    "created_by": args.created_by,
                },
            )
            row = cur.fetchone()
            if row is None:
                print("failed to insert payout correction", file=sys.stderr)
                return 1
            correction_id = int(row["id"])
        conn.commit()
        stored = _load_correction(conn, correction_id)
    assert stored is not None
    _emit_json(
        {
            "mode": "create-draft",
            "written": True,
            "correction": correction.row_to_correction_dict(stored),
        }
    )
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    del args
    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(correction.build_corrections_list_sql())
            rows = cur.fetchall()
    _emit_json(
        {
            "mode": "list",
            "corrections": [correction.row_to_correction_dict(row) for row in rows],
        }
    )
    return 0


def _cmd_details(args: argparse.Namespace) -> int:
    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        row = _load_correction(conn, args.correction_id)
    if row is None:
        print(f"payout correction not found: {args.correction_id}", file=sys.stderr)
        return 1
    _emit_json(
        {
            "mode": "details",
            "correction": correction.row_to_correction_dict(row),
        }
    )
    return 0


def _cmd_cancel(args: argparse.Namespace) -> int:
    with psycopg.connect(_database_url()) as conn:
        existing = _load_correction(conn, args.correction_id)
        refusal = correction.evaluate_cancel_correction_refusal(existing)
        if refusal and (
            existing is None
            or str(existing.get("status")) != correction.CORRECTION_STATUS_CANCELLED
        ):
            print(refusal, file=sys.stderr)
            _emit_json(
                {
                    "mode": "cancel",
                    "cancelled": False,
                    "refusal_reason": refusal,
                }
            )
            return 1
        if existing is not None and str(existing.get("status")) == correction.CORRECTION_STATUS_CANCELLED:
            _emit_json(
                {
                    "mode": "cancel",
                    "cancelled": True,
                    "idempotent_replay": True,
                    "correction": correction.row_to_correction_dict(existing),
                }
            )
            return 0
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                correction.build_cancel_correction_sql(),
                {
                    "correction_id": args.correction_id,
                    "status": correction.CORRECTION_STATUS_CANCELLED,
                },
            )
            if cur.fetchone() is None:
                print("failed to cancel payout correction", file=sys.stderr)
                return 1
        conn.commit()
        stored = _load_correction(conn, args.correction_id)
    assert stored is not None
    _emit_json(
        {
            "mode": "cancel",
            "cancelled": True,
            "idempotent_replay": False,
            "correction": correction.row_to_correction_dict(stored),
        }
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.mode == "create-draft":
        return _cmd_create_draft(args)
    if args.mode == "list":
        return _cmd_list(args)
    if args.mode == "details":
        return _cmd_details(args)
    if args.mode == "cancel":
        return _cmd_cancel(args)
    print(f"unknown mode: {args.mode}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
