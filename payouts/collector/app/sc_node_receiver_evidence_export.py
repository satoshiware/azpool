from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

from payouts.collector.app import reward_events

ALLOWED_RPC_METHODS = frozenset({"listtransactions", "gettransaction"})

_DENIED_RECEIVER_EVIDENCE_WALLETS = frozenset({"wallet", "SUPPORT"})

_FORBIDDEN_RPC_METHOD = re.compile(
    r"\b("
    r"sendmany|sendtoaddress|sendrawtransaction|walletpassphrase|"
    r"getbalances|createrawtransaction|createwallet|loadwallet|"
    r"dumpprivkey|signrawtransaction|importprivkey|importwallet|"
    r"encryptwallet|backupwallet|privkey|dumpwallet"
    r")\b",
    re.IGNORECASE,
)

_SECRET_JSON_KEYS = frozenset(
    {
        "hex",
        "scriptSig",
        "redeemScript",
        "witness",
        "pubkey",
        "privkey",
        "seed",
        "mnemonic",
    }
)


def parse_wallet_allowlist(raw: str | None = None) -> frozenset[str]:
    """Return configured receiver-evidence wallet allowlist."""
    if raw is None:
        raw = os.environ.get("PAYOUT_RECEIVER_EVIDENCE_WALLET_ALLOWLIST", "")
    wallets = {item.strip() for item in str(raw).split(",") if item.strip()}
    return frozenset(wallets)


def normalize_wallet_name(value: str) -> str:
    trimmed = str(value).strip()
    if not trimmed:
        raise ValueError("wallet name is required")
    reward_events.assert_no_wallet_send_keywords(trimmed)
    return trimmed


def assert_allowed_rpc_method(method: str) -> None:
    normalized = str(method).strip().lower()
    if not normalized:
        raise ValueError("RPC method is required")
    if _FORBIDDEN_RPC_METHOD.search(normalized):
        raise ValueError(f"RPC method is forbidden: {normalized}")
    if normalized not in ALLOWED_RPC_METHODS:
        raise ValueError(f"RPC method is not allowlisted: {normalized}")


def assert_receiver_evidence_wallet_allowed(
    wallet: str,
    *,
    allowlist: frozenset[str] | None = None,
) -> None:
    normalized = normalize_wallet_name(wallet)
    if normalized in _DENIED_RECEIVER_EVIDENCE_WALLETS:
        raise ValueError(
            f"wallet {normalized!r} is denied for receiver evidence export "
            "(use SC-node listener wallets only)"
        )
    configured = parse_wallet_allowlist() if allowlist is None else allowlist
    if not configured:
        raise ValueError(
            "PAYOUT_RECEIVER_EVIDENCE_WALLET_ALLOWLIST must include the target wallet"
        )
    if normalized not in configured:
        raise ValueError(f"wallet {normalized!r} is not in receiver evidence allowlist")


def build_listtransactions_argv(
    *,
    azc_bin: str,
    wallet: str,
    count: int,
    allowlist: frozenset[str] | None = None,
) -> list[str]:
    assert_receiver_evidence_wallet_allowed(wallet, allowlist=allowlist)
    reward_events.assert_no_wallet_send_keywords(azc_bin)
    assert_allowed_rpc_method("listtransactions")
    if count < 1 or count > 10_000:
        raise ValueError("count must be between 1 and 10000")
    argv = [
        azc_bin,
        f"-rpcwallet={normalize_wallet_name(wallet)}",
        "listtransactions",
        "*",
        str(int(count)),
        "0",
    ]
    for arg in argv:
        reward_events.assert_no_wallet_send_keywords(arg)
        if _FORBIDDEN_RPC_METHOD.search(arg):
            raise ValueError("argv must not contain forbidden RPC keywords")
    return argv


def build_gettransaction_argv(
    *,
    azc_bin: str,
    wallet: str,
    txid: str,
    allowlist: frozenset[str] | None = None,
) -> list[str]:
    assert_receiver_evidence_wallet_allowed(wallet, allowlist=allowlist)
    reward_events.assert_no_wallet_send_keywords(azc_bin)
    assert_allowed_rpc_method("gettransaction")
    normalized_txid = str(txid).strip()
    if not normalized_txid:
        raise ValueError("txid is required")
    argv = [
        azc_bin,
        f"-rpcwallet={normalize_wallet_name(wallet)}",
        "gettransaction",
        normalized_txid,
    ]
    for arg in argv:
        reward_events.assert_no_wallet_send_keywords(arg)
        if _FORBIDDEN_RPC_METHOD.search(arg):
            raise ValueError("argv must not contain forbidden RPC keywords")
    return argv


def sanitize_transaction_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return a reconciliation-safe wallet row with secret-like fields removed."""
    sanitized: dict[str, Any] = {}
    for key, value in row.items():
        if key in _SECRET_JSON_KEYS:
            continue
        if isinstance(value, Mapping):
            sanitized[key] = sanitize_transaction_row(value)
        elif isinstance(value, list):
            sanitized[key] = [
                sanitize_transaction_row(item)
                if isinstance(item, Mapping)
                else item
                for item in value
            ]
        else:
            sanitized[key] = value
    return sanitized


def filter_receive_transactions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for row in rows:
        category = str(row.get("category", "")).strip().lower()
        if category == "receive":
            filtered.append(dict(row))
    return filtered


def build_receiver_evidence_export(
    *,
    wallet: str,
    transactions: list[dict[str, Any]],
    count: int,
    receive_only: bool,
    txid_details: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    selected = filter_receive_transactions(transactions) if receive_only else transactions
    payload: dict[str, Any] = {
        "export_kind": "sc_node_receiver_evidence",
        "wallet": normalize_wallet_name(wallet),
        "count_requested": int(count),
        "receive_only": bool(receive_only),
        "transaction_count": len(selected),
        "transactions": [sanitize_transaction_row(row) for row in selected],
    }
    if txid_details is not None:
        payload["txid_details"] = [sanitize_transaction_row(row) for row in txid_details]
    return payload


def validate_export_json(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, sort_keys=True)
    parsed = json.loads(encoded)
    if not isinstance(parsed, dict):
        raise ValueError("export payload must serialize to a JSON object")
    if "transactions" not in parsed or not isinstance(parsed["transactions"], list):
        raise ValueError("export payload must include transactions array")
