#!/usr/bin/env python3
"""SC-node payout plan preview/write (no-send proposals only)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from payouts.collector.app import sc_node_payout_correction as payout_correction
from payouts.collector.app import sc_node_payout_planner as planner


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SC-node payout plan generator (no wallet RPC, no sends)"
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--credit-run-id", type=int, required=True)
    common.add_argument("--wallet", required=True, help="Support wallet name")
    common.add_argument(
        "--trusted-balance-snapshot",
        required=True,
        help="Operator-supplied current trusted wallet balance",
    )
    common.add_argument(
        "--reserve-fraction",
        default=str(planner.DEFAULT_RESERVE_FRACTION),
        help="Reserve fraction of trusted balance (default 0.50)",
    )
    common.add_argument("--notes", default=None, help="Optional operator notes")
    common.add_argument(
        "--payout-correction-id",
        type=int,
        default=None,
        help="Optional draft payout correction to apply as an audited offset",
    )

    subparsers.add_parser(
        "preview",
        parents=[common],
        help="Preview payout plan JSON without writing",
    )
    subparsers.add_parser(
        "write-draft",
        parents=[common],
        help="Insert draft payout plan and rows",
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


def _load_preview(
    conn: psycopg.Connection,
    *,
    credit_run_id: int,
    wallet_name: str,
    trusted_balance_snapshot: Decimal,
    reserve_fraction: Decimal,
    payout_correction_id: int | None = None,
) -> planner.PayoutPlanPreview:
    params = {"credit_run_id": credit_run_id}
    correction_row: dict[str, object] | None = None
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(planner.build_credit_run_for_plan_sql(), params)
        credit_run = cur.fetchone()
        cur.execute(planner.build_credits_for_plan_sql(), params)
        credits = cur.fetchall()
        cur.execute(planner.build_existing_draft_plan_sql(), params)
        existing = cur.fetchone()
        existing_plan_id = (
            int(existing["id"]) if existing is not None else None
        )
        if payout_correction_id is not None:
            cur.execute(
                payout_correction.build_correction_details_sql(payout_correction_id)
            )
            row = cur.fetchone()
            correction_row = dict(row) if row is not None else None

        address_lookup: dict[str, list[dict[str, object]]] = {}
        for credit in credits:
            sc_node_id = str(credit["sc_node_id"])
            if sc_node_id in address_lookup:
                continue
            cur.execute(
                planner.build_active_default_payout_addresses_sql(),
                {"sc_node_id": sc_node_id},
            )
            address_lookup[sc_node_id] = list(cur.fetchall())

    return planner.build_payout_plan_preview(
        credit_run_id=credit_run_id,
        wallet_name=wallet_name,
        reserve_fraction=reserve_fraction,
        trusted_balance_snapshot=trusted_balance_snapshot,
        credit_run=credit_run,
        credits=credits,
        address_lookup=address_lookup,
        existing_draft_plan_id=existing_plan_id,
        correction=correction_row,
    )


def _cmd_preview(args: argparse.Namespace) -> int:
    wallet_name = planner.normalize_wallet_name(args.wallet)
    trusted_balance = planner.parse_decimal_amount(
        args.trusted_balance_snapshot,
        field_name="trusted_balance_snapshot",
    )
    reserve_fraction = planner.parse_reserve_fraction(args.reserve_fraction)

    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        preview = _load_preview(
            conn,
            credit_run_id=args.credit_run_id,
            wallet_name=wallet_name,
            trusted_balance_snapshot=trusted_balance,
            reserve_fraction=reserve_fraction,
            payout_correction_id=args.payout_correction_id,
        )

    _emit_json({"mode": "preview", **planner.payout_plan_preview_to_dict(preview)})
    return 0


def _cmd_write_draft(args: argparse.Namespace) -> int:
    wallet_name = planner.normalize_wallet_name(args.wallet)
    trusted_balance = planner.parse_decimal_amount(
        args.trusted_balance_snapshot,
        field_name="trusted_balance_snapshot",
    )
    reserve_fraction = planner.parse_reserve_fraction(args.reserve_fraction)

    with psycopg.connect(_database_url()) as conn:
        preview = _load_preview(
            conn,
            credit_run_id=args.credit_run_id,
            wallet_name=wallet_name,
            trusted_balance_snapshot=trusted_balance,
            reserve_fraction=reserve_fraction,
            payout_correction_id=args.payout_correction_id,
        )
        if not preview.plan_allowed:
            print(preview.refusal_reason or "payout plan refused", file=sys.stderr)
            _emit_json(
                {
                    "mode": "write-draft",
                    "written": False,
                    **planner.payout_plan_preview_to_dict(preview),
                }
            )
            return 1

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                planner.build_insert_payout_plan_sql(),
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
                    "notes": args.notes,
                    "payout_correction_id": preview.payout_correction_id,
                },
            )
            plan_row = cur.fetchone()
            if plan_row is None:
                print("failed to insert payout plan", file=sys.stderr)
                return 1
            payout_plan_id = int(plan_row["id"])

            if preview.payout_correction_id is not None:
                cur.execute(
                    payout_correction.build_apply_correction_sql(),
                    {
                        "correction_id": preview.payout_correction_id,
                        "related_payout_plan_id": payout_plan_id,
                        "status": payout_correction.CORRECTION_STATUS_APPLIED,
                    },
                )
                if cur.fetchone() is None:
                    print("failed to apply payout correction", file=sys.stderr)
                    return 1

            for row in preview.rows:
                cur.execute(
                    planner.build_insert_payout_plan_row_sql(),
                    {
                        "payout_plan_id": payout_plan_id,
                        "credit_id": row.credit_id,
                        "sc_node_id": row.sc_node_id,
                        "sc_node_display_name": row.sc_node_display_name,
                        "payout_address": row.payout_address,
                        "gross_credit_amount": row.gross_credit_amount,
                        "correction_amount": row.correction_amount,
                        "payout_amount": row.payout_amount,
                        "row_status": "draft",
                    },
                )
        conn.commit()

    _emit_json(
        {
            "mode": "write-draft",
            "written": True,
            "payout_plan_id": payout_plan_id,
            **planner.payout_plan_preview_to_dict(preview),
        }
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.mode == "preview":
        return _cmd_preview(args)
    if args.mode == "write-draft":
        return _cmd_write_draft(args)
    print(f"unknown mode: {args.mode}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
