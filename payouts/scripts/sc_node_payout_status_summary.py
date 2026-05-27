#!/usr/bin/env python3
"""Compact read-only payout execution + active reconciliation status summary."""

from __future__ import annotations

import argparse
import json
import os
import sys

import psycopg
from psycopg.rows import dict_row

from payouts.collector.app import sc_node_payout_status_summary as status_summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only payout execution and active reconciliation summary"
    )
    parser.add_argument(
        "--production-execution-id",
        type=int,
        required=True,
        help="Production execution id to summarize",
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
        raise SystemExit(0) from None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    production_execution_id = int(args.production_execution_id)
    database_url = _database_url()

    with psycopg.connect(database_url) as conn:
        conn.set_read_only(True)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                status_summary.build_payout_status_summary_execution_sql(
                    production_execution_id
                )
            )
            execution = cur.fetchone()
            if execution is None:
                print(
                    f"production execution not found: {production_execution_id}",
                    file=sys.stderr,
                )
                return 1

            cur.execute(
                status_summary.build_payout_status_summary_rows_sql(production_execution_id)
            )
            execution_rows = list(cur.fetchall())

            cur.execute(
                status_summary.build_payout_status_summary_chunks_sql(production_execution_id)
            )
            chunks = list(cur.fetchall())

            cur.execute(
                status_summary.build_payout_status_summary_active_chunked_reconciliation_sql(),
                {"production_execution_id": production_execution_id},
            )
            active_chunked_reconciliation = cur.fetchone()

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

    payload = status_summary.build_payout_status_summary(
        execution=execution,
        execution_rows=execution_rows,
        chunks=chunks,
        active_chunked_reconciliation=active_chunked_reconciliation,
        single_reconciliation_row=single_reconciliation_row,
    )
    _emit_json(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
