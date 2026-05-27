#!/usr/bin/env python3
"""Read-only payout cycle closeout / automation readiness gate."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psycopg
from psycopg.rows import dict_row

from payouts.collector.app import sc_node_payout_cycle_readiness as readiness
from payouts.collector.app import sc_node_payout_status_summary as status_summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only payout cycle readiness / closeout verdict"
    )
    parser.add_argument(
        "--production-execution-id",
        type=int,
        required=True,
        help="Production execution id to evaluate",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (default: operator text)",
    )
    return parser.parse_args(argv)


def _database_url() -> str:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        raise SystemExit(1)
    return database_url


def _load_context(
    cur: psycopg.Cursor,
    production_execution_id: int,
) -> tuple[dict[str, object], int, dict[str, object] | None] | None:
    cur.execute(
        status_summary.build_payout_status_summary_execution_sql(production_execution_id)
    )
    execution = cur.fetchone()
    if execution is None:
        return None

    cur.execute(status_summary.build_payout_status_summary_rows_sql(production_execution_id))
    execution_rows = list(cur.fetchall())

    cur.execute(status_summary.build_payout_status_summary_chunks_sql(production_execution_id))
    chunks = list(cur.fetchall())

    cur.execute(
        status_summary.build_payout_status_summary_active_chunked_reconciliation_sql(),
        {"production_execution_id": production_execution_id},
    )
    active_chunked_reconciliation = cur.fetchone()

    cur.execute(
        readiness.build_active_chunked_reconciliation_count_sql(),
        {"production_execution_id": production_execution_id},
    )
    count_row = cur.fetchone()
    active_count = int(count_row["active_count"]) if count_row is not None else 0

    single_reconciliation_row = None
    execution_txid = str(execution.get("txid") or "").strip()
    if execution_txid:
        cur.execute(
            status_summary.build_payout_status_summary_single_reconciliation_sql(),
            {
                "production_execution_id": production_execution_id,
                "txid": execution_txid,
            },
        )
        single_reconciliation_row = cur.fetchone()

    preflight = None
    preflight_id = execution.get("production_preflight_id")
    if preflight_id is not None:
        cur.execute(
            readiness.build_cycle_readiness_preflight_sql(int(preflight_id)),
        )
        preflight = cur.fetchone()

    summary = status_summary.build_payout_status_summary(
        execution=execution,
        execution_rows=execution_rows,
        chunks=chunks,
        active_chunked_reconciliation=active_chunked_reconciliation,
        single_reconciliation_row=single_reconciliation_row,
    )
    return summary, active_count, preflight


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    production_execution_id = int(args.production_execution_id)
    database_url = _database_url()

    with psycopg.connect(database_url) as conn:
        conn.set_read_only(True)
        with conn.cursor(row_factory=dict_row) as cur:
            loaded = _load_context(cur, production_execution_id)
            if loaded is None:
                print(
                    f"production execution not found: {production_execution_id}",
                    file=sys.stderr,
                )
                return 1
            summary, active_count, preflight = loaded

    report = readiness.evaluate_payout_cycle_readiness(
        summary=summary,
        active_chunked_reconciliation_count=active_count,
        preflight=preflight,
    )

    if args.json:
        try:
            json.dump(report, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
        except BrokenPipeError:
            return readiness.verdict_exit_code(str(report["verdict"]))
    else:
        try:
            sys.stdout.write(readiness.format_readiness_text(report))
        except BrokenPipeError:
            return readiness.verdict_exit_code(str(report["verdict"]))

    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
