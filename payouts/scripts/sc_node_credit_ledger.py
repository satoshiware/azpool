#!/usr/bin/env python3
"""SC-node credit ledger preview and draft writes (no wallet RPC, no sends)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from payouts.collector.app import sc_node_credit_ledger as ledger


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SC-node credit ledger preview/write/print (no wallet RPC, no sends)"
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    coverage_group = argparse.ArgumentParser(add_help=False)
    coverage_group.add_argument(
        "--coverage-start",
        default=None,
        help="Operator-selected coverage start (ISO-8601 timestamptz)",
    )
    coverage_group.add_argument(
        "--coverage-end",
        default=None,
        help="Operator-selected coverage end (ISO-8601 timestamptz)",
    )

    preview_parser = subparsers.add_parser(
        "preview",
        parents=[coverage_group],
        help="Preview credit allocation for a wallet (JSON only)",
    )
    preview_parser.add_argument("--wallet", required=True, help="Support wallet name")

    write_parser = subparsers.add_parser(
        "write-draft",
        parents=[coverage_group],
        help="Insert draft credit run, credits, and reward-event links",
    )
    write_parser.add_argument("--wallet", required=True, help="Support wallet name")
    write_parser.add_argument("--run-label", required=True, help="Operator label for run")
    write_parser.add_argument(
        "--allow-default-coverage",
        action="store_true",
        help="Allow default intersection coverage without explicit bounds",
    )
    write_parser.add_argument("--notes", default=None, help="Optional operator notes")

    print_parser = subparsers.add_parser("print", help="Read stored credit runs from Postgres")
    print_parser.add_argument(
        "--credit-run-id",
        type=int,
        default=None,
        help="Optional single credit run id for details",
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


def _coverage_params(coverage: ledger.CreditCoverage) -> dict[str, datetime]:
    return {
        "coverage_start": coverage.coverage_start,
        "coverage_end": coverage.coverage_end,
    }


def _fetch_bounds(
    conn: psycopg.Connection,
    *,
    wallet_name: str,
) -> tuple[datetime | None, datetime | None, datetime | None, datetime | None]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(ledger.build_pool_work_coverage_sql())
        pool_row = cur.fetchone() or {}
        cur.execute(
            ledger.build_mature_reward_coverage_sql(),
            {"wallet_name": wallet_name},
        )
        reward_row = cur.fetchone() or {}
    return (
        pool_row.get("pool_coverage_start"),
        pool_row.get("pool_coverage_end"),
        reward_row.get("reward_coverage_start"),
        reward_row.get("reward_coverage_end"),
    )


def _resolve_coverage(
    *,
    wallet_name: str,
    pool_start: datetime | None,
    pool_end: datetime | None,
    reward_start: datetime | None,
    reward_end: datetime | None,
    coverage_start_arg: str | None,
    coverage_end_arg: str | None,
) -> ledger.CreditCoverage:
    if coverage_start_arg or coverage_end_arg:
        if not coverage_start_arg or not coverage_end_arg:
            raise ValueError(
                "both --coverage-start and --coverage-end are required together"
            )
        return ledger.resolve_operator_coverage(
            coverage_start=ledger.parse_coverage_timestamp(
                coverage_start_arg,
                field_name="coverage_start",
            ),
            coverage_end=ledger.parse_coverage_timestamp(
                coverage_end_arg,
                field_name="coverage_end",
            ),
            pool_coverage_start=pool_start,
            pool_coverage_end=pool_end,
            reward_coverage_start=reward_start,
            reward_coverage_end=reward_end,
        )
    default = ledger.resolve_default_coverage(
        pool_coverage_start=pool_start,
        pool_coverage_end=pool_end,
        reward_coverage_start=reward_start,
        reward_coverage_end=reward_end,
    )
    if default is None:
        raise ValueError(
            "unable to derive default coverage: missing pool telemetry or mature rewards"
        )
    return default


def _load_preview(
    conn: psycopg.Connection,
    *,
    wallet_name: str,
    coverage: ledger.CreditCoverage,
) -> ledger.CreditRunPreview:
    params = {"wallet_name": wallet_name, **_coverage_params(coverage)}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(ledger.build_eligible_mature_rewards_sql(), params)
        reward_rows = cur.fetchall()
        cur.execute(ledger.build_sc_node_work_share_sql(), params)
        sc_node_rows = cur.fetchall()
        cur.execute(ledger.build_unmapped_work_sql(), params)
        unmapped_row = cur.fetchone()
    return ledger.build_credit_run_preview(
        wallet_name=wallet_name,
        coverage=coverage,
        reward_rows=reward_rows,
        sc_node_rows=sc_node_rows,
        unmapped_row=unmapped_row,
    )


def _cmd_preview(args: argparse.Namespace) -> int:
    wallet_name = ledger.normalize_wallet_name(args.wallet)
    database_url = _database_url()
    with psycopg.connect(database_url) as conn:
        conn.set_read_only(True)
        bounds = _fetch_bounds(conn, wallet_name=wallet_name)
        coverage = _resolve_coverage(
            wallet_name=wallet_name,
            pool_start=bounds[0],
            pool_end=bounds[1],
            reward_start=bounds[2],
            reward_end=bounds[3],
            coverage_start_arg=args.coverage_start,
            coverage_end_arg=args.coverage_end,
        )
        preview = _load_preview(conn, wallet_name=wallet_name, coverage=coverage)
    payload = {
        "mode": "preview",
        "accounting_note": (
            "support_wallet_reward_events is gross reward-event history, not wallet balance"
        ),
        **ledger.credit_run_preview_to_dict(preview),
    }
    _emit_json(payload)
    return 0 if preview.allocation_allowed else 0


def _cmd_write_draft(args: argparse.Namespace) -> int:
    wallet_name = ledger.normalize_wallet_name(args.wallet)
    explicit_coverage = bool(args.coverage_start and args.coverage_end)
    database_url = _database_url()

    with psycopg.connect(database_url) as conn:
        bounds = _fetch_bounds(conn, wallet_name=wallet_name)
        coverage = _resolve_coverage(
            wallet_name=wallet_name,
            pool_start=bounds[0],
            pool_end=bounds[1],
            reward_start=bounds[2],
            reward_end=bounds[3],
            coverage_start_arg=args.coverage_start,
            coverage_end_arg=args.coverage_end,
        )
        coverage_refusal = ledger.evaluate_write_draft_coverage_refusal(
            coverage=coverage,
            explicit_coverage=explicit_coverage,
            allow_default_coverage=args.allow_default_coverage,
        )
        if coverage_refusal:
            print(coverage_refusal, file=sys.stderr)
            return 1

        preview = _load_preview(conn, wallet_name=wallet_name, coverage=coverage)
        if not preview.allocation_allowed:
            print(preview.refusal_reason or "allocation refused", file=sys.stderr)
            payload = {
                "mode": "write-draft",
                "written": False,
                **ledger.credit_run_preview_to_dict(preview),
            }
            _emit_json(payload)
            return 1

        params = {"wallet_name": wallet_name, **_coverage_params(coverage)}
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(ledger.build_eligible_mature_rewards_sql(), params)
            reward_rows = cur.fetchall()

            cur.execute(
                ledger.build_insert_credit_run_sql(),
                {
                    "run_label": args.run_label,
                    "wallet_name": wallet_name,
                    "maturity_status": ledger.CREDIT_MATURITY_STATUS,
                    "coverage_start": coverage.coverage_start,
                    "coverage_end": coverage.coverage_end,
                    "reward_event_count": preview.reward_event_count,
                    "reward_amount_total": preview.reward_amount_total,
                    "mapped_work_total": preview.mapped_work_total,
                    "unmapped_work_total": preview.unmapped_work.work_delta_total,
                    "status": "draft",
                    "notes": args.notes,
                },
            )
            run_row = cur.fetchone()
            if run_row is None:
                print("failed to insert credit run", file=sys.stderr)
                return 1
            credit_run_id = int(run_row["id"])

            for credit in preview.sc_node_credits:
                cur.execute(
                    ledger.build_insert_credit_sql(),
                    {
                        "credit_run_id": credit_run_id,
                        "sc_node_id": credit.sc_node_id,
                        "reward_amount_total": preview.reward_amount_total,
                        "work_delta_total": credit.work_delta_total,
                        "work_share": credit.work_share,
                        "credit_amount": credit.credit_amount,
                        "credit_status": "draft",
                    },
                )

            for reward_row in reward_rows:
                cur.execute(
                    ledger.build_insert_credit_run_event_sql(),
                    {
                        "credit_run_id": credit_run_id,
                        "reward_event_id": int(reward_row["reward_event_id"]),
                    },
                )
        conn.commit()

    result = ledger.credit_run_preview_to_dict(preview)
    _emit_json(
        {
            "mode": "write-draft",
            "written": True,
            "credit_run_id": credit_run_id,
            **result,
        }
    )
    return 0


def _cmd_print(args: argparse.Namespace) -> int:
    database_url = _database_url()
    with psycopg.connect(database_url) as conn:
        conn.set_read_only(True)
        with conn.cursor(row_factory=dict_row) as cur:
            if args.credit_run_id is None:
                cur.execute(ledger.build_credit_runs_sql())
                runs = [ledger.row_to_credit_run_dict(row) for row in cur.fetchall()]
                _emit_json({"mode": "print", "credit_runs": runs})
                return 0

            cur.execute(ledger.build_credit_run_details_sql(args.credit_run_id))
            run_row = cur.fetchone()
            if run_row is None:
                print(f"credit run not found: {args.credit_run_id}", file=sys.stderr)
                return 1
            cur.execute(ledger.build_credit_run_credits_sql(args.credit_run_id))
            credits = [ledger.row_to_credit_dict(row) for row in cur.fetchall()]
            cur.execute(ledger.build_credit_run_events_sql(args.credit_run_id))
            events = [ledger.row_to_credit_run_event_dict(row) for row in cur.fetchall()]

    _emit_json(
        {
            "mode": "print",
            "credit_run": ledger.row_to_credit_run_dict(run_row),
            "credits": credits,
            "reward_events": events,
        }
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.mode == "preview":
        return _cmd_preview(args)
    if args.mode == "write-draft":
        return _cmd_write_draft(args)
    if args.mode == "print":
        return _cmd_print(args)
    print(f"unknown mode: {args.mode}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
