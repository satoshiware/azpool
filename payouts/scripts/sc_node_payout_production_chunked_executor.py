#!/usr/bin/env python3
"""Chunked production SC-node payout executor (sendtoaddress per chunk only)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row

from payouts.collector.app import payout_addresses
from payouts.collector.app import sc_node_payout_planner as planner
from payouts.collector.app import sc_node_payout_production_chunked_executor as chunked
from payouts.collector.app import sc_node_payout_production_executor as executor


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Chunked production payout executor (sequential sendtoaddress)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--payout-plan-id", type=int, required=True)
    common.add_argument("--production-preflight-id", type=int, required=True)
    common.add_argument("--source-wallet-name", required=True)
    common.add_argument("--chunk-amount", required=True)
    common.add_argument(
        "--azc-bin",
        default=os.environ.get("AZC_BIN", "azc"),
        help="Wallet CLI binary",
    )

    subparsers.add_parser("preview", parents=[common], help="Preview chunked execution")
    execute_parser = subparsers.add_parser(
        "execute-real",
        parents=[common],
        help="Execute chunked sendtoaddress sequence",
    )
    execute_parser.add_argument("--idempotency-key", required=True)
    execute_parser.add_argument("--confirm-phrase", required=True)
    execute_parser.add_argument("--notes", default=None)

    confirm_parser = subparsers.add_parser("mark-confirmed", help="Confirm chunked execution")
    confirm_parser.add_argument("--production-execution-id", type=int, required=True)

    details_parser = subparsers.add_parser("details", help="Chunked execution details")
    details_parser.add_argument("--production-execution-id", type=int, required=True)

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


def _chunk_amount_arg(value: str) -> Decimal:
    return chunked.normalize_chunk_amount(value)


def _getbalances_argv(*, azc_bin: str, source_wallet_name: str) -> list[str]:
    executor.assert_no_forbidden_wallet_rpc_keywords_except_sendtoaddress(azc_bin)
    argv = [azc_bin, f"-rpcwallet={source_wallet_name}", "getbalances"]
    for arg in argv:
        executor.assert_no_forbidden_wallet_rpc_keywords_except_sendtoaddress(arg)
    return argv


def _run_getbalances(*, azc_bin: str, source_wallet_name: str) -> dict[str, Any]:
    completed = subprocess.run(
        _getbalances_argv(azc_bin=azc_bin, source_wallet_name=source_wallet_name),
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "getbalances failed").strip()
        print(message, file=sys.stderr)
        raise SystemExit(completed.returncode)
    parsed = json.loads(completed.stdout)
    if not isinstance(parsed, dict):
        print("getbalances must return a JSON object", file=sys.stderr)
        raise SystemExit(1)
    return parsed


def _run_sendtoaddress(
    *,
    azc_bin: str,
    source_wallet_name: str,
    payout_address: str,
    payout_amount: Decimal,
) -> str:
    argv = chunked.build_sendtoaddress_argv(
        azc_bin=azc_bin,
        source_wallet_name=source_wallet_name,
        payout_address=payout_address,
        payout_amount=payout_amount,
    )
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "sendtoaddress failed").strip()
        raise RuntimeError(message)
    txid = completed.stdout.strip()
    if not txid:
        raise RuntimeError("sendtoaddress returned empty txid")
    return txid


def _address_lookup(conn: psycopg.Connection) -> dict[str, list[dict[str, object]]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(payout_addresses.build_active_default_payout_addresses_sql())
        rows = cur.fetchall()
    lookup: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        sc_node_id = str(row["sc_node_id"])
        lookup.setdefault(sc_node_id, []).append(dict(row))
    return lookup


def _load_bundle(
    conn: psycopg.Connection,
    *,
    payout_plan_id: int,
    production_preflight_id: int,
) -> tuple[
    dict[str, object] | None,
    list[dict[str, object]],
    dict[str, object] | None,
    list[dict[str, object]],
    dict[str, list[dict[str, object]]],
]:
    params = {
        "payout_plan_id": payout_plan_id,
        "production_preflight_id": production_preflight_id,
    }
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(executor.build_approved_payout_plan_for_execution_sql(payout_plan_id))
        plan = cur.fetchone()
        cur.execute(executor.build_approved_payout_plan_rows_for_execution_sql(payout_plan_id))
        plan_rows = list(cur.fetchall())
        cur.execute(executor.build_passed_production_preflight_sql(), params)
        preflight = cur.fetchone()
        cur.execute(executor.build_production_preflight_rows_for_execution_sql(), params)
        preflight_rows = list(cur.fetchall())
    return plan, plan_rows, preflight, preflight_rows, _address_lookup(conn)


def _load_details(
    conn: psycopg.Connection,
    production_execution_id: int,
) -> tuple[dict[str, object] | None, list[dict[str, object]], list[dict[str, object]]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(executor.build_production_execution_details_sql(production_execution_id))
        header = cur.fetchone()
        cur.execute(executor.build_production_execution_rows_sql(production_execution_id))
        rows = list(cur.fetchall())
        cur.execute(chunked.build_production_execution_chunks_sql(production_execution_id))
        chunks = list(cur.fetchall())
    return header, rows, chunks


def _details_payload(
    header: dict[str, object] | None,
    rows: list[dict[str, object]],
    chunks: list[dict[str, object]],
    *,
    command: str,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "command": command,
        "production_execution": executor.row_to_production_execution_dict(header),
        "rows": [executor.row_to_production_execution_row_dict(row) for row in rows],
        "chunks": [chunked.row_to_production_execution_chunk_dict(row) for row in chunks],
    }
    if extra:
        payload.update(extra)
    return payload


def _cmd_preview(args: argparse.Namespace) -> int:
    source_wallet = executor.normalize_source_wallet_name(args.source_wallet_name)
    chunk_amount = _chunk_amount_arg(args.chunk_amount)
    balance_payload = _run_getbalances(azc_bin=args.azc_bin, source_wallet_name=source_wallet)
    wallet_balance = executor.parse_wallet_balance_from_getbalances(balance_payload)

    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        plan, plan_rows, preflight, preflight_rows, address_lookup = _load_bundle(
            conn,
            payout_plan_id=args.payout_plan_id,
            production_preflight_id=args.production_preflight_id,
        )
        preview = chunked.build_chunked_execution_preview(
            payout_plan_id=args.payout_plan_id,
            production_preflight_id=args.production_preflight_id,
            source_wallet_name=source_wallet,
            chunk_amount=chunk_amount,
            plan=plan,
            plan_rows=plan_rows,
            preflight=preflight,
            preflight_rows=preflight_rows,
            wallet_balance=wallet_balance,
            address_lookup=address_lookup,
        )

    _emit_json({"command": "preview", **chunked.chunked_execution_preview_to_dict(preview)})
    return 0


def _insert_refused(
    cur: psycopg.Cursor,
    *,
    args: argparse.Namespace,
    source_wallet: str,
    confirmation_phrase: str,
    idempotency_key: str,
    preview: chunked.ChunkedExecutionPreview,
    refusal_reason: str,
) -> int:
    notes = chunked.build_chunked_executor_notes(preview.chunk_amount)
    if args.notes:
        notes = f"{notes}; {args.notes}"
    cur.execute(
        executor.build_insert_production_execution_sql(),
        {
            "payout_plan_id": args.payout_plan_id,
            "production_preflight_id": args.production_preflight_id,
            "source_wallet_name": source_wallet,
            "status": executor.EXECUTION_STATUS_REFUSED,
            "planned_amount_total": preview.planned_amount_total,
            "trusted_balance_before": preview.wallet_balance.trusted,
            "immature_balance_before": preview.wallet_balance.immature,
            "reserve_amount": preview.reserve_amount,
            "spendable_after_reserve": preview.spendable_after_reserve,
            "execution_attempt_count": 0,
            "idempotency_key": idempotency_key,
            "confirmation_phrase": confirmation_phrase,
            "txid": None,
            "refusal_reason": refusal_reason,
            "notes": notes,
        },
    )
    inserted = cur.fetchone()
    if inserted is None:
        raise RuntimeError("failed to insert refused chunked execution")
    execution_id = int(inserted["id"])
    for row in preview.rows:
        cur.execute(
            executor.build_insert_production_execution_row_sql(),
            {
                "production_execution_id": execution_id,
                "payout_plan_row_id": row.payout_plan_row_id,
                "sc_node_id": row.sc_node_id,
                "payout_address": row.payout_address,
                "payout_amount": row.payout_amount,
                "row_status": executor.ROW_STATUS_REFUSED,
                "txid": None,
                "refusal_reason": refusal_reason,
            },
        )
    return execution_id


def _cmd_execute_real(args: argparse.Namespace) -> int:
    source_wallet = executor.normalize_source_wallet_name(args.source_wallet_name)
    chunk_amount = _chunk_amount_arg(args.chunk_amount)
    idempotency_key = executor.normalize_idempotency_key(args.idempotency_key)
    confirmation_phrase = executor.normalize_confirmation_phrase(args.confirm_phrase)
    balance_payload = _run_getbalances(azc_bin=args.azc_bin, source_wallet_name=source_wallet)
    wallet_balance = executor.parse_wallet_balance_from_getbalances(balance_payload)

    with psycopg.connect(_database_url()) as conn:
        plan, plan_rows, preflight, preflight_rows, address_lookup = _load_bundle(
            conn,
            payout_plan_id=args.payout_plan_id,
            production_preflight_id=args.production_preflight_id,
        )
        preview = chunked.build_chunked_execution_preview(
            payout_plan_id=args.payout_plan_id,
            production_preflight_id=args.production_preflight_id,
            source_wallet_name=source_wallet,
            chunk_amount=chunk_amount,
            plan=plan,
            plan_rows=plan_rows,
            preflight=preflight,
            preflight_rows=preflight_rows,
            wallet_balance=wallet_balance,
            address_lookup=address_lookup,
        )
        params = {"payout_plan_id": args.payout_plan_id, "idempotency_key": idempotency_key}
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(chunked.build_execution_by_plan_idempotency_sql(), params)
            existing = cur.fetchone()
            cur.execute(chunked.build_existing_active_production_execution_sql(), params)
            active = cur.fetchone()

        if existing is not None:
            header, rows, chunks = _load_details(conn, int(existing["id"]))
            _emit_json(
                _details_payload(
                    header,
                    rows,
                    chunks,
                    command="execute-real",
                    extra={
                        "executed": str(header.get("status") if header else "")
                        in {
                            executor.EXECUTION_STATUS_SENT,
                            chunked.EXECUTION_STATUS_PARTIAL_SENT,
                        },
                        "idempotent_replay": True,
                        "production_execution_id": int(existing["id"]),
                    },
                )
            )
            return 0

        refusal = chunked.evaluate_chunked_execute_real_refusal(
            plan=plan,
            plan_rows=plan_rows,
            preflight=preflight,
            preflight_rows=preflight_rows,
            source_wallet_name=source_wallet,
            wallet_balance=wallet_balance,
            address_lookup=address_lookup,
            confirmation_phrase=confirmation_phrase,
            chunk_amount=chunk_amount,
            chunks=preview.chunks,
            existing_by_key=existing,
            active_execution=active,
            idempotency_key=idempotency_key,
        )
        if refusal:
            with conn.cursor(row_factory=dict_row) as cur:
                execution_id = _insert_refused(
                    cur,
                    args=args,
                    source_wallet=source_wallet,
                    confirmation_phrase=confirmation_phrase,
                    idempotency_key=idempotency_key,
                    preview=preview,
                    refusal_reason=refusal,
                )
            conn.commit()
            header, rows, chunks = _load_details(conn, execution_id)
            print(refusal, file=sys.stderr)
            _emit_json(
                _details_payload(
                    header,
                    rows,
                    chunks,
                    command="execute-real",
                    extra={
                        "executed": False,
                        "refusal_reason": refusal,
                        "production_execution_id": execution_id,
                    },
                )
            )
            return 1

        notes = chunked.build_chunked_executor_notes(chunk_amount)
        if args.notes:
            notes = f"{notes}; {args.notes}"

        row_id_by_plan_row: dict[int, int] = {}
        chunk_db_rows: list[dict[str, object]] = []

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                executor.build_insert_production_execution_sql(),
                {
                    "payout_plan_id": args.payout_plan_id,
                    "production_preflight_id": args.production_preflight_id,
                    "source_wallet_name": source_wallet,
                    "status": executor.EXECUTION_STATUS_DRAFT,
                    "planned_amount_total": preview.planned_amount_total,
                    "trusted_balance_before": preview.wallet_balance.trusted,
                    "immature_balance_before": preview.wallet_balance.immature,
                    "reserve_amount": preview.reserve_amount,
                    "spendable_after_reserve": preview.spendable_after_reserve,
                    "execution_attempt_count": 0,
                    "idempotency_key": idempotency_key,
                    "confirmation_phrase": confirmation_phrase,
                    "txid": None,
                    "refusal_reason": None,
                    "notes": notes,
                },
            )
            inserted = cur.fetchone()
            if inserted is None:
                print("failed to insert chunked production execution", file=sys.stderr)
                return 1
            execution_id = int(inserted["id"])

            for row in plan_rows:
                cur.execute(
                    executor.build_insert_production_execution_row_sql(),
                    {
                        "production_execution_id": execution_id,
                        "payout_plan_row_id": int(row["id"]),
                        "sc_node_id": str(row["sc_node_id"]),
                        "payout_address": str(row["payout_address"]),
                        "payout_amount": planner._to_decimal(row["payout_amount"]),
                        "row_status": executor.ROW_STATUS_DRAFT,
                        "txid": None,
                        "refusal_reason": None,
                    },
                )
                row_inserted = cur.fetchone()
                if row_inserted is not None:
                    row_id_by_plan_row[int(row["id"])] = int(row_inserted["id"])

            for chunk in preview.chunks:
                execution_row_id = row_id_by_plan_row[chunk.payout_plan_row_id]
                cur.execute(
                    chunked.build_insert_production_execution_chunk_sql(),
                    {
                        "production_execution_id": execution_id,
                        "production_execution_row_id": execution_row_id,
                        "payout_plan_id": args.payout_plan_id,
                        "payout_plan_row_id": chunk.payout_plan_row_id,
                        "sc_node_id": chunk.sc_node_id,
                        "payout_address": chunk.payout_address,
                        "chunk_index": chunk.chunk_index,
                        "chunk_amount": chunk.chunk_amount,
                        "chunk_status": chunked.CHUNK_STATUS_DRAFT,
                        "txid": None,
                        "refusal_reason": None,
                    },
                )
                chunk_inserted = cur.fetchone()
                if chunk_inserted is not None:
                    chunk_db_rows.append(
                        {
                            "id": int(chunk_inserted["id"]),
                            "chunk": chunk,
                        }
                    )

        first_txid: str | None = None
        sent_count = 0
        try:
            for chunk_row in chunk_db_rows:
                chunk_id = int(chunk_row["id"])
                chunk_plan: chunked.ChunkPlan = chunk_row["chunk"]
                txid = _run_sendtoaddress(
                    azc_bin=args.azc_bin,
                    source_wallet_name=source_wallet,
                    payout_address=chunk_plan.payout_address,
                    payout_amount=chunk_plan.chunk_amount,
                )
                if first_txid is None:
                    first_txid = txid
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        chunked.build_mark_chunk_sent_sql(),
                        {"chunk_id": chunk_id, "txid": txid},
                    )
                    if cur.fetchone() is None:
                        raise RuntimeError("failed to mark chunk sent")
                sent_count += 1
        except RuntimeError as exc:
            with conn.cursor(row_factory=dict_row) as cur:
                if sent_count < len(chunk_db_rows):
                    cur.execute(
                        chunked.build_mark_chunk_refused_sql(),
                        {
                            "chunk_id": int(chunk_db_rows[sent_count]["id"]),
                            "refusal_reason": str(exc),
                        },
                    )
                cur.execute(
                    chunked.build_mark_execution_partial_sent_sql(),
                    {
                        "production_execution_id": execution_id,
                        "refusal_reason": str(exc),
                    },
                )
            conn.commit()
            header, rows, chunks = _load_details(conn, execution_id)
            print(str(exc), file=sys.stderr)
            _emit_json(
                _details_payload(
                    header,
                    rows,
                    chunks,
                    command="execute-real",
                    extra={
                        "executed": False,
                        "partial_sent": True,
                        "refusal_reason": str(exc),
                        "production_execution_id": execution_id,
                        "chunks_sent": sent_count,
                    },
                )
            )
            return 1

        assert first_txid is not None
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                chunked.build_mark_chunked_execution_sent_sql(),
                {"production_execution_id": execution_id, "txid": first_txid},
            )
            if cur.fetchone() is None:
                print("failed to mark chunked production execution sent", file=sys.stderr)
                return 1
            for execution_row_id in row_id_by_plan_row.values():
                cur.execute(
                    chunked.build_mark_chunked_execution_row_sent_sql(),
                    {
                        "production_execution_row_id": execution_row_id,
                        "txid": first_txid,
                    },
                )
        conn.commit()

        header, rows, chunks = _load_details(conn, execution_id)
        _emit_json(
            _details_payload(
                header,
                rows,
                chunks,
                command="execute-real",
                extra={
                    "executed": True,
                    "idempotent_replay": False,
                    "production_execution_id": execution_id,
                    "first_txid": first_txid,
                },
            )
        )
    return 0


def _cmd_mark_confirmed(args: argparse.Namespace) -> int:
    with psycopg.connect(_database_url()) as conn:
        header, rows, chunks = _load_details(conn, args.production_execution_id)
        sent_chunk_count = sum(
            1 for chunk in chunks if str(chunk.get("chunk_status")) == chunked.CHUNK_STATUS_SENT
        )
        refusal = chunked.evaluate_chunked_mark_confirmed_refusal(
            header,
            chunk_count=len(chunks),
            sent_chunk_count=sent_chunk_count,
        )
        if header is not None and str(header.get("status")) == executor.EXECUTION_STATUS_CONFIRMED:
            _emit_json(
                _details_payload(
                    header,
                    rows,
                    chunks,
                    command="mark-confirmed",
                    extra={"confirmed": True, "idempotent_replay": True},
                )
            )
            return 0
        if refusal:
            print(refusal, file=sys.stderr)
            _emit_json(
                {
                    "command": "mark-confirmed",
                    "confirmed": False,
                    "refusal_reason": refusal,
                }
            )
            return 1

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                chunked.build_mark_chunked_execution_confirmed_sql(),
                {"production_execution_id": args.production_execution_id},
            )
            if cur.fetchone() is None:
                print("failed to confirm chunked production execution", file=sys.stderr)
                return 1
            cur.execute(
                chunked.build_mark_chunks_confirmed_sql(),
                {"production_execution_id": args.production_execution_id},
            )
            cur.execute(
                executor.build_mark_production_execution_rows_confirmed_sql(),
                {"production_execution_id": args.production_execution_id},
            )
        conn.commit()

        header, rows, chunks = _load_details(conn, args.production_execution_id)
        _emit_json(
            _details_payload(
                header,
                rows,
                chunks,
                command="mark-confirmed",
                extra={"confirmed": True, "idempotent_replay": False},
            )
        )
    return 0


def _cmd_details(args: argparse.Namespace) -> int:
    with psycopg.connect(_database_url()) as conn:
        conn.set_read_only(True)
        header, rows, chunks = _load_details(conn, args.production_execution_id)
        if header is None:
            print(
                f"production execution not found: {args.production_execution_id}",
                file=sys.stderr,
            )
            return 1
        _emit_json(_details_payload(header, rows, chunks, command="details"))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "preview":
        return _cmd_preview(args)
    if args.command == "execute-real":
        return _cmd_execute_real(args)
    if args.command == "mark-confirmed":
        return _cmd_mark_confirmed(args)
    if args.command == "details":
        return _cmd_details(args)
    print(f"unknown command: {args.command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
