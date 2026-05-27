#!/usr/bin/env python3
"""Read-only SC-node listener wallet receiver evidence export (listtransactions only)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from payouts.collector.app import sc_node_receiver_evidence_export as export


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export SC-node listener wallet receive-side evidence via read-only azc RPC"
        )
    )
    parser.add_argument(
        "--wallet",
        required=True,
        help="Explicit SC-node listener wallet name (-rpcwallet)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=500,
        help="listtransactions count (1-10000)",
    )
    parser.add_argument(
        "--receive-only",
        action="store_true",
        help="Export only category=receive rows (recommended for payout reconciliation)",
    )
    parser.add_argument(
        "--txid",
        action="append",
        default=[],
        help="Optional txid to include gettransaction detail (repeatable)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write JSON to this path (default: stdout)",
    )
    parser.add_argument(
        "--azc-bin",
        default=os.environ.get("AZC_BIN", "/usr/local/bin/azc-payout-readonly"),
        help="Read-only azc wrapper (listtransactions/gettransaction only)",
    )
    return parser.parse_args(argv)


def _run_rpc(argv: list[str], *, label: str) -> Any:
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or f"{label} failed").strip()
        print(message, file=sys.stderr)
        raise SystemExit(completed.returncode)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        print(f"invalid JSON from {label}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _run_listtransactions(*, azc_bin: str, wallet: str, count: int) -> list[dict[str, Any]]:
    argv = export.build_listtransactions_argv(azc_bin=azc_bin, wallet=wallet, count=count)
    parsed = _run_rpc(argv, label="listtransactions")
    if not isinstance(parsed, list):
        print("listtransactions must return a JSON array", file=sys.stderr)
        raise SystemExit(1)
    return [row for row in parsed if isinstance(row, dict)]


def _run_gettransaction(*, azc_bin: str, wallet: str, txid: str) -> dict[str, Any]:
    argv = export.build_gettransaction_argv(azc_bin=azc_bin, wallet=wallet, txid=txid)
    parsed = _run_rpc(argv, label="gettransaction")
    if not isinstance(parsed, dict):
        print("gettransaction must return a JSON object", file=sys.stderr)
        raise SystemExit(1)
    return parsed


def _emit_json(payload: dict[str, Any], output_path: str | None) -> None:
    export.validate_export_json(payload)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if output_path is None:
        try:
            sys.stdout.write(encoded)
        except BrokenPipeError:
            raise SystemExit(0) from None
        return
    path = Path(output_path)
    path.write_text(encoded, encoding="utf-8")
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"written output is not valid JSON: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    transactions = _run_listtransactions(
        azc_bin=args.azc_bin,
        wallet=args.wallet,
        count=args.count,
    )
    txid_details: list[dict[str, Any]] | None = None
    if args.txid:
        txid_details = []
        for txid in args.txid:
            txid_details.append(
                _run_gettransaction(azc_bin=args.azc_bin, wallet=args.wallet, txid=txid)
            )
    payload = export.build_receiver_evidence_export(
        wallet=args.wallet,
        transactions=transactions,
        count=args.count,
        receive_only=args.receive_only,
        txid_details=txid_details,
    )
    _emit_json(payload, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
