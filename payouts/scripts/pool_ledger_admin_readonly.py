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
}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only pool-ledger admin queries")
    parser.add_argument(
        "command",
        choices=["pool-instances", "sc-nodes", "mappings", "unmapped-identities"],
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=admin_readonly.DEFAULT_UNMAPPED_LIMIT,
        help="Row limit for unmapped-identities (1-500)",
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

    if args.command == "unmapped-identities":
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
        sql = build_sql()
        rows = _run_query(database_url, sql, row_fn)
        payload = {"command": args.command, "rows": rows}

    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
