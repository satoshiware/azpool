#!/usr/bin/env python3
"""Chunked SC-node payout reconciliation (read-only gettransaction; no sends)."""

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

from payouts.collector.app import sc_node_chunked_payout_reconciliation as chunked_recon
from payouts.collector.app.sc_node_payout_reconciliation import SourceTransactionEvidence


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Chunked payout reconciliation (gettransaction only; no sends)"
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

    subparsers.add_parser("preview", parents=[common], help="Preview (no DB writes)")
    record_parser = subparsers.add_parser(
        "record",
        parents=[common],
        help="Record reconciliation audit rows",
    )
    record_parser.add_argument(
        "--supersede-reconciliation-id",
        type=int,
        default=None,
        help="Active reconciliation id to supersede when retrying after stale evidence",
    )
    record_parser.add_argument(
        "--supersede-reason",
        default=None,
        help="Audit reason recorded on the superseded reconciliation row",
    )

    details_parser = subparsers.add_parser("details", help="Show recorded reconciliation")
    details_parser.add_argument("--reconciliation-id", type=int, required=True)
    details_parser.add_argument(
        "--include-raw-evidence",
        action="store_true",
        help="Include full stored wallet evidence (default omits hex)",
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


def _gettransaction_argv(
    *,
    azc_bin: str,
    source_wallet_name: str,
    txid: str,
) -> list[str]:
    chunked_recon.assert_no_wallet_send_keywords(azc_bin)
    argv = [
        azc_bin,
        f"-rpcwallet={source_wallet_name}",
        "gettransaction",
        txid,
    ]
    for arg in argv:
        chunked_recon.assert_no_wallet_send_keywords(arg)
    return argv


def _run_gettransaction(
    *,
    azc_bin: str,
    source_wallet_name: str,
    txid: str,
) -> dict[str, Any]:
    completed = subprocess.run(
        _gettransaction_argv(
            azc_bin=azc_bin,
            source_wallet_name=source_wallet_name,
            txid=txid,
        ),
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


def _load_receiver_rows(path: str | None) -> list[dict[str, Any]] | None:
    if path is None:
        return None
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
            chunked_recon.build_confirmed_chunked_execution_sql(production_execution_id)
        )
        execution = cur.fetchone()
        cur.execute(
            chunked_recon.build_confirmed_execution_chunks_sql(production_execution_id)
        )
        chunks = list(cur.fetchall())
    return execution, chunks


def _fetch_source_by_txid(
    *,
    azc_bin: str,
    source_wallet_name: str,
    chunks: list[dict[str, object]],
) -> dict[str, SourceTransactionEvidence]:
    source_by_txid: dict[str, SourceTransactionEvidence] = {}
    for chunk in chunks:
        txid = str(chunk["txid"]).strip()
        if not txid:
            continue
        payload = _run_gettransaction(
            azc_bin=azc_bin,
            source_wallet_name=source_wallet_name,
            txid=txid,
        )
        source_by_txid[txid] = chunked_recon.parse_source_gettransaction(payload, txid)
    return source_by_txid


def _build_preview(
    *,
    execution: dict[str, object],
    chunks: list[dict[str, object]],
    source_wallet_name: str,
    source_by_txid: dict[str, SourceTransactionEvidence],
    receiver_rows: list[dict[str, Any]] | None,
) -> chunked_recon.ChunkedReconciliationPreview:
    return chunked_recon.build_chunked_reconciliation_preview(
        execution=execution,
        chunks=chunks,
        source_wallet_name=source_wallet_name,
        source_by_txid=source_by_txid,
        receiver_rows=receiver_rows,
    )


def _jsonb_evidence(value: dict[str, Any] | None) -> Jsonb | None:
    if value is None:
        return None
    return Jsonb(value)


def _insert_chunked_reconciliation(
    cur: psycopg.Cursor,
    preview: chunked_recon.ChunkedReconciliationPreview,
) -> int:
    cur.execute(
        chunked_recon.build_insert_chunked_reconciliation_sql(),
        {
            "production_execution_id": preview.production_execution_id,
            "payout_plan_id": preview.payout_plan_id,
            "sc_node_id": preview.sc_node_id,
            "payout_address": preview.payout_address,
            "expected_chunk_count": preview.expected_chunk_count,
            "source_chunk_count": preview.source_chunk_count,
            "receiver_chunk_count": preview.receiver_chunk_count,
            "expected_amount_total": preview.expected_amount_total,
            "source_amount_total": preview.source_amount_total,
            "source_fee_total": preview.source_fee_total,
            "receiver_amount_total": preview.receiver_amount_total,
            "reconciliation_status": preview.reconciliation_status,
            "matched": preview.matched,
            "refusal_reason": preview.mismatch_reason,
            "source_wallet_name": preview.source_wallet_name,
            "source_wallet_evidence": _jsonb_evidence(preview.source_wallet_evidence),
            "receiver_wallet_evidence": _jsonb_evidence(preview.receiver_wallet_evidence),
        },
    )
    inserted = cur.fetchone()
    if inserted is None:
        raise RuntimeError("failed to insert chunked reconciliation")
    reconciliation_id = int(inserted["id"])
    for chunk_row in preview.chunks:
        cur.execute(
            chunked_recon.build_insert_chunked_reconciliation_chunk_sql(),
            {
                "reconciliation_id": reconciliation_id,
                "production_execution_chunk_id": chunk_row.production_execution_chunk_id,
                "chunk_index": chunk_row.chunk_index,
                "txid": chunk_row.txid,
                "expected_amount": chunk_row.expected_amount,
                "source_amount": chunk_row.source_amount,
                "source_fee": chunk_row.source_fee,
                "source_confirmations": chunk_row.source_confirmations,
                "source_blockhash": chunk_row.source_blockhash,
                "receiver_amount": chunk_row.receiver_amount,
                "receiver_address": chunk_row.receiver_address,
                "receiver_confirmations": chunk_row.receiver_confirmations,
                "receiver_category": chunk_row.receiver_category,
                "row_status": chunk_row.row_status,
                "refusal_reason": chunk_row.mismatch_reason,
            },
        )
    return reconciliation_id


def _load_reconciliation_bundle(
    conn: psycopg.Connection,
    reconciliation_id: int,
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(chunked_recon.build_chunked_reconciliation_details_sql(reconciliation_id))
        header = cur.fetchone()
        cur.execute(chunked_recon.build_chunked_reconciliation_chunks_sql(reconciliation_id))
        chunks = list(cur.fetchall())
    return header, chunks


def _cmd_preview(args: argparse.Namespace) -> int:
    source_wallet = chunked_recon.normalize_source_wallet_name(args.source_wallet_name)

    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        execution, chunks = _load_execution_bundle(conn, args.production_execution_id)
        if execution is None:
            print(
                f"confirmed production execution not found: {args.production_execution_id}",
                file=sys.stderr,
            )
            return 1
        if not chunks:
            print("no confirmed chunks found for production execution", file=sys.stderr)
            return 1

    source_by_txid = _fetch_source_by_txid(
        azc_bin=args.azc_bin,
        source_wallet_name=source_wallet,
        chunks=chunks,
    )
    receiver_rows = _load_receiver_rows(args.receiver_transactions_json)
    preview = _build_preview(
        execution=execution,
        chunks=chunks,
        source_wallet_name=source_wallet,
        source_by_txid=source_by_txid,
        receiver_rows=receiver_rows,
    )
    _emit_json(
        {
            "command": "preview",
            "recorded": False,
            **chunked_recon.chunked_reconciliation_preview_to_dict(preview),
        }
    )
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    source_wallet = chunked_recon.normalize_source_wallet_name(args.source_wallet_name)

    with psycopg.connect(_database_url()) as conn:
        execution, chunks = _load_execution_bundle(conn, args.production_execution_id)
        if execution is None:
            print(
                f"confirmed production execution not found: {args.production_execution_id}",
                file=sys.stderr,
            )
            return 1
        if not chunks:
            print("no confirmed chunks found for production execution", file=sys.stderr)
            return 1

        source_by_txid = _fetch_source_by_txid(
            azc_bin=args.azc_bin,
            source_wallet_name=source_wallet,
            chunks=chunks,
        )
        receiver_rows = _load_receiver_rows(args.receiver_transactions_json)
        preview = _build_preview(
            execution=execution,
            chunks=chunks,
            source_wallet_name=source_wallet,
            source_by_txid=source_by_txid,
            receiver_rows=receiver_rows,
        )

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                chunked_recon.build_lock_active_chunked_reconciliation_by_execution_sql(),
                {"production_execution_id": args.production_execution_id},
            )
            existing = cur.fetchone()

            if existing is not None:
                reconciliation_id = int(existing["id"])
                refusal = chunked_recon.preview_matches_existing_chunked_reconciliation(
                    preview,
                    existing,
                )
                if refusal is None:
                    header, chunk_rows = _load_reconciliation_bundle(
                        conn,
                        reconciliation_id,
                    )
                    _emit_json(
                        {
                            "command": "record",
                            "recorded": False,
                            "idempotent_replay": True,
                            "reconciliation_id": reconciliation_id,
                            "reconciliation": chunked_recon.row_to_chunked_reconciliation_dict(
                                header
                            ),
                            "chunks": [
                                chunked_recon.row_to_chunked_reconciliation_chunk_dict(
                                    row
                                )
                                for row in chunk_rows
                            ],
                        }
                    )
                    return 0

                supersede_refusal = (
                    chunked_recon.validate_chunked_reconciliation_supersede_request(
                        supersede_reconciliation_id=args.supersede_reconciliation_id,
                        supersede_reason=args.supersede_reason,
                        active_reconciliation=existing,
                        production_execution_id=args.production_execution_id,
                    )
                )
                if supersede_refusal is not None:
                    _emit_json(
                        {
                            "command": "record",
                            "recorded": False,
                            "idempotent_replay": False,
                            "superseded": False,
                            "reconciliation_id": reconciliation_id,
                            "refusal_reason": supersede_refusal,
                        }
                    )
                    return 1

                superseded_id = reconciliation_id
                cur.execute(
                    chunked_recon.build_mark_chunked_reconciliation_superseded_sql(),
                    {
                        "reconciliation_id": superseded_id,
                        "production_execution_id": args.production_execution_id,
                        "superseded_reason": args.supersede_reason.strip(),
                    },
                )
                if cur.fetchone() is None:
                    print("failed to mark prior reconciliation superseded", file=sys.stderr)
                    return 1
                try:
                    new_reconciliation_id = _insert_chunked_reconciliation(cur, preview)
                except Exception as exc:
                    conn.rollback()
                    print(
                        f"failed to insert replacement reconciliation: {exc}",
                        file=sys.stderr,
                    )
                    return 1
                cur.execute(
                    chunked_recon.build_link_chunked_reconciliation_superseded_by_sql(),
                    {
                        "reconciliation_id": superseded_id,
                        "production_execution_id": args.production_execution_id,
                        "superseded_by_reconciliation_id": new_reconciliation_id,
                    },
                )
                if cur.fetchone() is None:
                    print(
                        "failed to link superseded reconciliation to replacement",
                        file=sys.stderr,
                    )
                    conn.rollback()
                    return 1
                reconciliation_id = new_reconciliation_id
                conn.commit()
                header, chunk_rows = _load_reconciliation_bundle(
                    conn,
                    reconciliation_id,
                )
                _emit_json(
                    {
                        "command": "record",
                        "recorded": True,
                        "idempotent_replay": False,
                        "superseded": True,
                        "supersede_reconciliation_id": superseded_id,
                        "reconciliation_id": reconciliation_id,
                        "reconciliation": chunked_recon.row_to_chunked_reconciliation_dict(
                            header
                        ),
                        "chunks": [
                            chunked_recon.row_to_chunked_reconciliation_chunk_dict(row)
                            for row in chunk_rows
                        ],
                    }
                )
                return 0

            reconciliation_id = _insert_chunked_reconciliation(cur, preview)
        conn.commit()

        header, chunk_rows = _load_reconciliation_bundle(conn, reconciliation_id)
        _emit_json(
            {
                "command": "record",
                "recorded": True,
                "idempotent_replay": False,
                "superseded": False,
                "reconciliation_id": reconciliation_id,
                "reconciliation": chunked_recon.row_to_chunked_reconciliation_dict(header),
                "chunks": [
                    chunked_recon.row_to_chunked_reconciliation_chunk_dict(row)
                    for row in chunk_rows
                ],
            }
        )
    return 0


def _cmd_details(args: argparse.Namespace) -> int:
    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        header, chunk_rows = _load_reconciliation_bundle(conn, args.reconciliation_id)
        if header is None:
            print(
                f"chunked reconciliation not found: {args.reconciliation_id}",
                file=sys.stderr,
            )
            return 1
        _emit_json(
            {
                "command": "details",
                "reconciliation": chunked_recon.row_to_chunked_reconciliation_dict(
                    header,
                    include_raw_evidence=args.include_raw_evidence,
                ),
                "chunks": [
                    chunked_recon.row_to_chunked_reconciliation_chunk_dict(row)
                    for row in chunk_rows
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
