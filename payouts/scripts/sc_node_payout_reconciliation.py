#!/usr/bin/env python3
"""SC-node payout post-execution reconciliation (read-only gettransaction; no sends)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from payouts.collector.app import sc_node_payout_reconciliation as reconciliation


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SC-node payout reconciliation (gettransaction only; no sends)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--production-execution-id", type=int, required=True)
    common.add_argument("--source-wallet-name", required=True)
    common.add_argument(
        "--azc-bin",
        default=os.environ.get("AZC_BIN", "azc"),
        help="Wallet CLI for read-only gettransaction",
    )
    common.add_argument(
        "--receiver-transactions-json",
        default=None,
        help="Path to SC-node wallet receive-side JSON export (optional)",
    )
    common.add_argument("--notes", default=None)

    subparsers.add_parser(
        "preview",
        parents=[common],
        help="Preview reconciliation (no DB writes)",
    )
    subparsers.add_parser(
        "record",
        parents=[common],
        help="Record reconciliation audit rows only",
    )

    details_parser = subparsers.add_parser("details", help="Show recorded reconciliation")
    details_parser.add_argument("--reconciliation-id", type=int, required=True)

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


def _gettransaction_argv(
    *,
    azc_bin: str,
    source_wallet_name: str,
    txid: str,
) -> list[str]:
    reconciliation.assert_no_wallet_send_keywords(azc_bin)
    argv = [
        azc_bin,
        f"-rpcwallet={source_wallet_name}",
        "gettransaction",
        txid,
    ]
    for arg in argv:
        reconciliation.assert_no_wallet_send_keywords(arg)
    return argv


def _run_gettransaction(
    *,
    azc_bin: str,
    source_wallet_name: str,
    txid: str,
) -> dict[str, Any]:
    argv = _gettransaction_argv(
        azc_bin=azc_bin,
        source_wallet_name=source_wallet_name,
        txid=txid,
    )
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "gettransaction failed").strip()
        print(message, file=sys.stderr)
        raise SystemExit(completed.returncode)
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        print(f"invalid JSON from gettransaction: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not isinstance(parsed, dict):
        print("gettransaction must return a JSON object", file=sys.stderr)
        raise SystemExit(1)
    return parsed


def _load_receiver_rows(path: str | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    file_path = Path(path)
    if not file_path.is_file():
        print(f"receiver transactions file not found: {path}", file=sys.stderr)
        raise SystemExit(1)
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"invalid receiver JSON: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        transactions = payload.get("transactions")
        if isinstance(transactions, list):
            return [row for row in transactions if isinstance(row, dict)]
    print("receiver JSON must be a list or object with transactions array", file=sys.stderr)
    raise SystemExit(1)


def _load_execution_bundle(
    conn: psycopg.Connection,
    production_execution_id: int,
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            reconciliation.build_confirmed_production_execution_sql(
                production_execution_id
            )
        )
        execution = cur.fetchone()
        cur.execute(
            reconciliation.build_confirmed_production_execution_rows_sql(
                production_execution_id
            )
        )
        rows = list(cur.fetchall())
    return execution, rows


def _jsonb_evidence(value: dict[str, Any] | None) -> Jsonb | None:
    if value is None:
        return None
    return Jsonb(value)


def _reconciliation_insert_params(
    preview: reconciliation.ReconciliationPreview,
    *,
    notes: str | None,
) -> dict[str, object]:
    """Build INSERT params with JSONB-adapted wallet evidence for psycopg."""
    return {
        "production_execution_id": preview.production_execution_id,
        "payout_plan_id": preview.payout_plan_id,
        "source_wallet_name": preview.source_wallet_name,
        "txid": preview.txid,
        "reconciliation_status": preview.reconciliation_status,
        "expected_amount": preview.expected_amount,
        "expected_address": preview.expected_address,
        "source_confirmations": preview.source_confirmations,
        "source_fee": preview.source_fee,
        "source_amount": preview.source_amount,
        "receiver_confirmations": preview.receiver_confirmations,
        "receiver_amount": preview.receiver_amount,
        "receiver_category": preview.receiver_category,
        "receiver_address": preview.receiver_address,
        "matched": preview.matched,
        "mismatch_reason": preview.mismatch_reason,
        "source_wallet_evidence": _jsonb_evidence(preview.source_wallet_evidence),
        "receiver_wallet_evidence": _jsonb_evidence(preview.receiver_wallet_evidence),
        "notes": notes,
    }


def _build_preview(
    *,
    execution: dict[str, object],
    execution_rows: list[dict[str, object]],
    source_payload: dict[str, Any],
    receiver_rows: list[dict[str, Any]],
) -> reconciliation.ReconciliationPreview:
    txid = str(execution["txid"])
    source_evidence = reconciliation.parse_source_gettransaction(source_payload, txid)
    receiver_evidence = reconciliation.parse_receiver_transactions_json(
        receiver_rows,
        txid,
    )
    return reconciliation.compare_reconciliation(
        execution,
        execution_rows,
        source_evidence,
        receiver_evidence,
    )


def _cmd_preview(args: argparse.Namespace) -> int:
    source_wallet = reconciliation.normalize_source_wallet_name(args.source_wallet_name)

    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        execution, execution_rows = _load_execution_bundle(
            conn,
            args.production_execution_id,
        )
        if execution is None:
            print(
                f"confirmed production execution not found: {args.production_execution_id}",
                file=sys.stderr,
            )
            return 1
        txid = str(execution.get("txid", "")).strip()
        if not txid:
            print("production execution has no txid", file=sys.stderr)
            return 1

    source_payload = _run_gettransaction(
        azc_bin=args.azc_bin,
        source_wallet_name=source_wallet,
        txid=txid,
    )
    receiver_rows = _load_receiver_rows(args.receiver_transactions_json)
    preview = _build_preview(
        execution=execution,
        execution_rows=execution_rows,
        source_payload=source_payload,
        receiver_rows=receiver_rows,
    )
    _emit_json(
        {
            "command": "preview",
            "recorded": False,
            **reconciliation.reconciliation_preview_to_dict(preview),
        }
    )
    return 0


def _load_reconciliation_bundle(
    conn: psycopg.Connection,
    reconciliation_id: int,
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(reconciliation.build_reconciliation_details_sql(reconciliation_id))
        header = cur.fetchone()
        cur.execute(reconciliation.build_reconciliation_rows_sql(reconciliation_id))
        rows = list(cur.fetchall())
    return header, rows


def _cmd_record(args: argparse.Namespace) -> int:
    source_wallet = reconciliation.normalize_source_wallet_name(args.source_wallet_name)

    with psycopg.connect(_database_url()) as conn:
        execution, execution_rows = _load_execution_bundle(
            conn,
            args.production_execution_id,
        )
        if execution is None:
            print(
                f"confirmed production execution not found: {args.production_execution_id}",
                file=sys.stderr,
            )
            return 1
        txid = str(execution.get("txid", "")).strip()
        if not txid:
            print("production execution has no txid", file=sys.stderr)
            return 1

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                reconciliation.build_reconciliation_by_execution_txid_sql(),
                {
                    "production_execution_id": args.production_execution_id,
                    "txid": txid,
                },
            )
            existing = cur.fetchone()
        if existing is not None:
            header, rows = _load_reconciliation_bundle(conn, int(existing["id"]))
            _emit_json(
                {
                    "command": "record",
                    "recorded": False,
                    "idempotent_replay": True,
                    "reconciliation": reconciliation.row_to_reconciliation_dict(header),
                    "rows": [
                        reconciliation.row_to_reconciliation_row_dict(row)
                        for row in rows
                    ],
                }
            )
            return 0

    source_payload = _run_gettransaction(
        azc_bin=args.azc_bin,
        source_wallet_name=source_wallet,
        txid=txid,
    )
    receiver_rows = _load_receiver_rows(args.receiver_transactions_json)
    preview = _build_preview(
        execution=execution,
        execution_rows=execution_rows,
        source_payload=source_payload,
        receiver_rows=receiver_rows,
    )

    with psycopg.connect(_database_url()) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                reconciliation.build_insert_reconciliation_sql(),
                _reconciliation_insert_params(preview, notes=args.notes),
            )
            inserted = cur.fetchone()
            if inserted is None:
                print("failed to insert reconciliation", file=sys.stderr)
                return 1
            reconciliation_id = int(inserted["id"])

            for row in preview.rows:
                cur.execute(
                    reconciliation.build_insert_reconciliation_row_sql(),
                    {
                        "reconciliation_id": reconciliation_id,
                        "production_execution_row_id": row.production_execution_row_id,
                        "sc_node_id": row.sc_node_id,
                        "expected_address": row.expected_address,
                        "expected_amount": row.expected_amount,
                        "receiver_address": row.receiver_address,
                        "receiver_amount": row.receiver_amount,
                        "receiver_category": row.receiver_category,
                        "receiver_confirmations": row.receiver_confirmations,
                        "row_status": row.row_status,
                        "mismatch_reason": row.mismatch_reason,
                    },
                )
        conn.commit()

        header, rows = _load_reconciliation_bundle(conn, reconciliation_id)
        assert header is not None
        _emit_json(
            {
                "command": "record",
                "recorded": True,
                "idempotent_replay": False,
                "reconciliation": reconciliation.row_to_reconciliation_dict(header),
                "rows": [
                    reconciliation.row_to_reconciliation_row_dict(row) for row in rows
                ],
            }
        )
    return 0


def _cmd_details(args: argparse.Namespace) -> int:
    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        header, rows = _load_reconciliation_bundle(conn, args.reconciliation_id)
        if header is None:
            print(
                f"reconciliation not found: {args.reconciliation_id}",
                file=sys.stderr,
            )
            return 1
        _emit_json(
            {
                "command": "details",
                "reconciliation": reconciliation.row_to_reconciliation_dict(header),
                "rows": [
                    reconciliation.row_to_reconciliation_row_dict(row) for row in rows
                ],
            }
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "preview":
        return _cmd_preview(args)
    if args.command == "record":
        return _cmd_record(args)
    if args.command == "details":
        return _cmd_details(args)
    print(f"unknown command: {args.command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
