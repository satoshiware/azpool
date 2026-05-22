#!/usr/bin/env python3
"""Read-only pool-ledger admin visibility (registry, SC nodes, mappings, unmapped identities)."""

from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg
from psycopg.rows import dict_row

from payouts.collector.app import admin_readonly


_COMMANDS: dict[str, tuple[str, object]] = {
    "pool-instances": (
        admin_readonly.build_pool_instances_sql,
        admin_readonly.row_to_pool_instance_dict,
    ),
    "sc-nodes": (
        admin_readonly.build_sc_nodes_sql,
        admin_readonly.row_to_sc_node_dict,
    ),
    "mappings": (
        admin_readonly.build_identity_mappings_sql,
        admin_readonly.row_to_identity_mapping_dict,
    ),
    "payout-addresses": (
        admin_readonly.build_payout_addresses_sql,
        admin_readonly.row_to_payout_address_dict,
    ),
    "reward-events": (
        admin_readonly.build_reward_events_sql,
        admin_readonly.row_to_reward_event_dict,
    ),
    "credit-runs": (
        admin_readonly.build_credit_runs_sql,
        admin_readonly.row_to_credit_run_dict,
    ),
    "payout-plans": (
        admin_readonly.build_payout_plans_sql,
        admin_readonly.row_to_payout_plan_dict,
    ),
    "payout-test-executions": (
        admin_readonly.build_payout_test_executions_sql,
        admin_readonly.row_to_payout_test_execution_dict,
    ),
    "production-preflights": (
        admin_readonly.build_production_preflights_sql,
        admin_readonly.row_to_production_preflight_dict,
    ),
    "production-executions": (
        admin_readonly.build_production_executions_sql,
        admin_readonly.row_to_production_execution_dict,
    ),
}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only pool-ledger admin queries")
    parser.add_argument(
        "command",
        choices=[
            "pool-instances",
            "sc-nodes",
            "mappings",
            "payout-addresses",
            "reward-events",
            "credit-runs",
            "credit-run-details",
            "payout-plans",
            "payout-plan-details",
            "payout-test-executions",
            "payout-test-execution-details",
            "production-preflights",
            "production-preflight-details",
            "production-executions",
            "production-execution-details",
            "unmapped-identities",
        ],
    )
    parser.add_argument(
        "--credit-run-id",
        type=int,
        default=None,
        help="Credit run id for credit-run-details",
    )
    parser.add_argument(
        "--payout-plan-id",
        type=int,
        default=None,
        help="Payout plan id for payout-plan-details",
    )
    parser.add_argument(
        "--test-execution-id",
        type=int,
        default=None,
        help="Test execution id for payout-test-execution-details",
    )
    parser.add_argument(
        "--production-preflight-id",
        type=int,
        default=None,
        help="Production preflight id for production-preflight-details",
    )
    parser.add_argument(
        "--production-execution-id",
        type=int,
        default=None,
        help="Production execution id for production-execution-details",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=admin_readonly.DEFAULT_UNMAPPED_LIMIT,
        help="Row limit for unmapped-identities (1-500)",
    )
    parser.add_argument(
        "--maturity-status",
        default=None,
        help="Filter reward-events by maturity_status",
    )
    return parser.parse_args(argv)


def _run_query(database_url: str, sql: str, row_fn: object) -> list[dict[str, object]]:
    admin_readonly.assert_readonly_sql(sql)
    with psycopg.connect(database_url) as conn:
        conn.set_read_only(True)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql)
            rows = cur.fetchall()
    return [row_fn(row) for row in rows]  # type: ignore[operator]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 1

    if args.command == "production-execution-details":
        if args.production_execution_id is None:
            print(
                "--production-execution-id is required for production-execution-details",
                file=sys.stderr,
            )
            return 1
        header_sql = admin_readonly.build_production_execution_details_sql(
            args.production_execution_id
        )
        rows_sql = admin_readonly.build_production_execution_rows_sql(
            args.production_execution_id
        )
        header_rows = _run_query(
            database_url,
            header_sql,
            admin_readonly.row_to_production_execution_dict,
        )
        if not header_rows:
            print(
                f"production execution not found: {args.production_execution_id}",
                file=sys.stderr,
            )
            return 1
        row_details = _run_query(
            database_url,
            rows_sql,
            admin_readonly.row_to_production_execution_row_dict,
        )
        payload = {
            "command": args.command,
            "production_execution_id": args.production_execution_id,
            "production_execution": header_rows[0],
            "rows": row_details,
        }
    elif args.command == "production-preflight-details":
        if args.production_preflight_id is None:
            print(
                "--production-preflight-id is required for production-preflight-details",
                file=sys.stderr,
            )
            return 1
        header_sql = admin_readonly.build_production_preflight_details_sql(
            args.production_preflight_id
        )
        rows_sql = admin_readonly.build_production_preflight_rows_sql(
            args.production_preflight_id
        )
        header_rows = _run_query(
            database_url,
            header_sql,
            admin_readonly.row_to_production_preflight_dict,
        )
        if not header_rows:
            print(
                f"production preflight not found: {args.production_preflight_id}",
                file=sys.stderr,
            )
            return 1
        row_details = _run_query(
            database_url,
            rows_sql,
            admin_readonly.row_to_production_preflight_row_dict,
        )
        payload = {
            "command": args.command,
            "production_preflight_id": args.production_preflight_id,
            "production_preflight": header_rows[0],
            "rows": row_details,
        }
    elif args.command == "payout-test-execution-details":
        if args.test_execution_id is None:
            print(
                "--test-execution-id is required for payout-test-execution-details",
                file=sys.stderr,
            )
            return 1
        header_sql = admin_readonly.build_payout_test_execution_details_sql(
            args.test_execution_id
        )
        rows_sql = admin_readonly.build_payout_test_execution_rows_sql(
            args.test_execution_id
        )
        header_rows = _run_query(
            database_url,
            header_sql,
            admin_readonly.row_to_payout_test_execution_dict,
        )
        if not header_rows:
            print(f"test execution not found: {args.test_execution_id}", file=sys.stderr)
            return 1
        row_details = _run_query(
            database_url,
            rows_sql,
            admin_readonly.row_to_payout_test_execution_row_dict,
        )
        payload = {
            "command": args.command,
            "test_execution_id": args.test_execution_id,
            "test_execution": header_rows[0],
            "rows": row_details,
        }
    elif args.command == "payout-plan-details":
        if args.payout_plan_id is None:
            print("--payout-plan-id is required for payout-plan-details", file=sys.stderr)
            return 1
        plan_sql = admin_readonly.build_payout_plan_details_sql(args.payout_plan_id)
        rows_sql = admin_readonly.build_payout_plan_rows_sql(args.payout_plan_id)
        plan_rows = _run_query(database_url, plan_sql, admin_readonly.row_to_payout_plan_dict)
        if not plan_rows:
            print(f"payout plan not found: {args.payout_plan_id}", file=sys.stderr)
            return 1
        plan_row_details = _run_query(
            database_url,
            rows_sql,
            admin_readonly.row_to_payout_plan_row_dict,
        )
        payload = {
            "command": args.command,
            "payout_plan_id": args.payout_plan_id,
            "payout_plan": plan_rows[0],
            "rows": plan_row_details,
        }
    elif args.command == "credit-run-details":
        if args.credit_run_id is None:
            print("--credit-run-id is required for credit-run-details", file=sys.stderr)
            return 1
        run_sql = admin_readonly.build_credit_run_details_sql(args.credit_run_id)
        credits_sql = admin_readonly.build_credit_run_credits_sql(args.credit_run_id)
        events_sql = admin_readonly.build_credit_run_events_sql(args.credit_run_id)
        run_rows = _run_query(database_url, run_sql, admin_readonly.row_to_credit_run_dict)
        if not run_rows:
            print(f"credit run not found: {args.credit_run_id}", file=sys.stderr)
            return 1
        credits = _run_query(database_url, credits_sql, admin_readonly.row_to_credit_dict)
        events = _run_query(
            database_url,
            events_sql,
            admin_readonly.row_to_credit_run_event_dict,
        )
        payload = {
            "command": args.command,
            "credit_run_id": args.credit_run_id,
            "credit_run": run_rows[0],
            "credits": credits,
            "reward_events": events,
        }
    elif args.command == "unmapped-identities":
        limit = admin_readonly.clamp_unmapped_limit(args.limit)
        sql = admin_readonly.build_unmapped_identities_sql(limit)
        rows = _run_query(database_url, sql, admin_readonly.row_to_unmapped_identity_dict)
        payload: dict[str, object] = {
            "command": args.command,
            "limit": limit,
            "rows": rows,
        }
    else:
        build_sql, row_fn = _COMMANDS[args.command]
        if args.command == "reward-events":
            sql = build_sql(args.maturity_status)
        else:
            sql = build_sql()
        rows = _run_query(database_url, sql, row_fn)
        payload = {"command": args.command, "rows": rows}
        if args.command == "reward-events" and args.maturity_status:
            payload["maturity_status"] = args.maturity_status

    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
