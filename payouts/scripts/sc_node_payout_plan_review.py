#!/usr/bin/env python3
"""SC-node payout plan approval, cancel, and no-send preflight (no wallet RPC)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from payouts.collector.app import sc_node_payout_plan_review as review
from payouts.collector.app.sc_node_payout_planner import (
    build_active_default_payout_addresses_sql,
    parse_decimal_amount,
    parse_reserve_fraction,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Payout plan approval/cancel/preflight (no sends)"
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    approve_parser = subparsers.add_parser("approve", help="Approve draft payout plan")
    approve_parser.add_argument("--payout-plan-id", type=int, required=True)
    approve_parser.add_argument("--approved-by", required=True)
    approve_parser.add_argument("--confirmation", required=True)
    approve_parser.add_argument("--approval-note", default=None)

    cancel_parser = subparsers.add_parser("cancel", help="Cancel draft or approved plan")
    cancel_parser.add_argument("--payout-plan-id", type=int, required=True)
    cancel_parser.add_argument("--cancelled-by", required=True)
    cancel_parser.add_argument("--reason", required=True)

    preflight_parser = subparsers.add_parser(
        "preflight",
        help="No-send preflight check for approved plan",
    )
    preflight_parser.add_argument("--payout-plan-id", type=int, required=True)
    preflight_parser.add_argument("--trusted-balance-current", required=True)
    preflight_parser.add_argument(
        "--reserve-fraction-current",
        default=None,
        help="Override reserve fraction (default: plan stored value)",
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


def _load_plan_bundle(
    conn: psycopg.Connection,
    payout_plan_id: int,
) -> tuple[dict[str, object] | None, list[dict[str, object]], dict[str, list[dict[str, object]]]]:
    params = {"payout_plan_id": payout_plan_id}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(review.build_payout_plan_for_review_sql(payout_plan_id))
        plan = cur.fetchone()
        cur.execute(review.build_payout_plan_rows_for_review_sql(payout_plan_id))
        plan_rows = list(cur.fetchall())
        address_lookup: dict[str, list[dict[str, object]]] = {}
        for row in plan_rows:
            sc_node_id = str(row["sc_node_id"])
            if sc_node_id in address_lookup:
                continue
            cur.execute(
                build_active_default_payout_addresses_sql(),
                {"sc_node_id": sc_node_id},
            )
            address_lookup[sc_node_id] = list(cur.fetchall())
    return plan, plan_rows, address_lookup


def _cmd_approve(args: argparse.Namespace) -> int:
    with psycopg.connect(_database_url()) as conn:
        plan, plan_rows, address_lookup = _load_plan_bundle(conn, args.payout_plan_id)
        refusal = review.evaluate_approve_refusal(
            plan=plan,
            plan_rows=plan_rows,
            address_lookup=address_lookup,
            confirmation=args.confirmation,
            payout_plan_id=args.payout_plan_id,
        )
        if refusal:
            print(refusal, file=sys.stderr)
            _emit_json(
                {
                    "mode": "approve",
                    "approved": False,
                    "payout_plan_id": args.payout_plan_id,
                    "refusal_reason": refusal,
                }
            )
            return 1

        assert plan is not None
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                review.build_update_approve_plan_sql(),
                {
                    "payout_plan_id": args.payout_plan_id,
                    "approved_by": args.approved_by,
                    "approval_note": args.approval_note,
                    "approval_confirmation_hash": review.hash_approval_confirmation(
                        args.confirmation
                    ),
                },
            )
            if cur.fetchone() is None:
                print("failed to approve payout plan", file=sys.stderr)
                return 1
            cur.execute(
                review.build_update_approve_rows_sql(),
                {"payout_plan_id": args.payout_plan_id},
            )
        conn.commit()

    _emit_json(
        {
            "mode": "approve",
            "approved": True,
            "payout_plan_id": args.payout_plan_id,
            "confirmation_phrase": review.build_approval_confirmation_phrase(
                args.payout_plan_id
            ),
        }
    )
    return 0


def _cmd_cancel(args: argparse.Namespace) -> int:
    with psycopg.connect(_database_url()) as conn:
        plan, plan_rows, _address_lookup = _load_plan_bundle(conn, args.payout_plan_id)
        refusal = review.evaluate_cancel_refusal(plan=plan, reason=args.reason)
        if refusal:
            print(refusal, file=sys.stderr)
            _emit_json(
                {
                    "mode": "cancel",
                    "cancelled": False,
                    "payout_plan_id": args.payout_plan_id,
                    "refusal_reason": refusal,
                }
            )
            return 1

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                review.build_update_cancel_plan_sql(),
                {
                    "payout_plan_id": args.payout_plan_id,
                    "cancelled_by": args.cancelled_by,
                    "cancellation_note": args.reason,
                },
            )
            if cur.fetchone() is None:
                print("failed to cancel payout plan", file=sys.stderr)
                return 1
            cur.execute(
                review.build_update_cancel_rows_sql(),
                {"payout_plan_id": args.payout_plan_id},
            )
        conn.commit()

    _emit_json(
        {
            "mode": "cancel",
            "cancelled": True,
            "payout_plan_id": args.payout_plan_id,
            "rows_updated": len(plan_rows),
        }
    )
    return 0


def _cmd_preflight(args: argparse.Namespace) -> int:
    trusted_current = parse_decimal_amount(
        args.trusted_balance_current,
        field_name="trusted_balance_current",
    )

    with psycopg.connect(_database_url()) as conn:
        plan, plan_rows, address_lookup = _load_plan_bundle(conn, args.payout_plan_id)
        if plan is None:
            print(f"payout plan not found: {args.payout_plan_id}", file=sys.stderr)
            return 1

        if args.reserve_fraction_current is not None:
            reserve_fraction = parse_reserve_fraction(args.reserve_fraction_current)
        else:
            reserve_fraction = Decimal(str(plan["reserve_fraction"]))

        result = review.build_preflight_result(
            payout_plan_id=args.payout_plan_id,
            plan=plan,
            plan_rows=plan_rows,
            trusted_balance_current=trusted_current,
            reserve_fraction_current=reserve_fraction,
            address_lookup=address_lookup,
        )

        preflight_status = (
            review.PREFLIGHT_STATUS_ALLOWED
            if result.preflight_allowed
            else review.PREFLIGHT_STATUS_REFUSED
        )
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                review.build_update_preflight_plan_sql(),
                {
                    "payout_plan_id": args.payout_plan_id,
                    "preflight_status": preflight_status,
                    "preflight_note": result.refusal_reason,
                },
            )
            if cur.fetchone() is None:
                print("failed to record preflight result", file=sys.stderr)
                return 1
        conn.commit()

    payload = {"mode": "preflight", **review.preflight_result_to_dict(result)}
    _emit_json(payload)
    return 0 if result.preflight_allowed else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.mode == "approve":
        return _cmd_approve(args)
    if args.mode == "cancel":
        return _cmd_cancel(args)
    if args.mode == "preflight":
        return _cmd_preflight(args)
    print(f"unknown mode: {args.mode}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
