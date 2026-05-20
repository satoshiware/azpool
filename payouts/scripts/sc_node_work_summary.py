#!/usr/bin/env python3
"""Read-only SC-node work summary report from pool telemetry deltas."""

from __future__ import annotations

import json
import os
import sys

import psycopg
from psycopg.rows import dict_row

from payouts.collector.app.sc_node_summary import (
    build_sc_node_summary_sql,
    build_unmapped_summary_sql,
    row_to_sc_node_summary_dict,
    row_to_unmapped_summary_dict,
)


def _empty_unmapped() -> dict[str, object]:
    return {
        "accepted_delta_total": "0",
        "work_delta_total": "0",
        "delta_rows": 0,
        "first_observed_at": None,
        "last_observed_at": None,
    }


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 1

    sc_node_sql = build_sc_node_summary_sql()
    unmapped_sql = build_unmapped_summary_sql()

    with psycopg.connect(database_url) as conn:
        conn.set_read_only(True)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sc_node_sql)
            sc_nodes = [row_to_sc_node_summary_dict(row) for row in cur.fetchall()]

            cur.execute(unmapped_sql)
            unmapped_rows = cur.fetchall()

    unmapped = (
        row_to_unmapped_summary_dict(unmapped_rows[0])
        if unmapped_rows
        else _empty_unmapped()
    )

    payload = {"sc_nodes": sc_nodes, "unmapped": unmapped}
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
