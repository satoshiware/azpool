from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

VALID_REWARD_MATURITY_STATUSES = frozenset(
    {"unknown", "immature", "mature", "orphaned", "conflicted", "abandoned"}
)
REWARD_EVENT_CATEGORIES = frozenset({"generate", "immature", "orphan"})
IGNORED_WALLET_CATEGORIES = frozenset({"receive", "send", "move"})
DEFAULT_MATURITY_CONFIRMATIONS = 100

_WALLET_SEND_KEYWORDS = re.compile(
    r"\b("
    r"sendmany|sendtoaddress|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|privkey"
    r")\b",
    re.IGNORECASE,
)

_READONLY_SQL_FORBIDDEN = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE|"
    r"GRANT|REVOKE|VACUUM|CALL"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SupportWalletRewardEvent:
    wallet_name: str | None
    txid: str
    vout: int | None
    category: str | None
    amount: Decimal
    confirmations: int
    blockhash: str | None
    blockheight: int | None
    blockindex: int | None
    blocktime: datetime | None
    event_time: datetime | None
    trusted: bool | None
    spendable: bool | None
    generated: bool | None
    immature: bool | None
    abandoned: bool | None
    maturity_status: str
    raw_wallet_event: dict[str, Any]


def assert_no_wallet_send_keywords(text: str) -> None:
    """Raise ValueError if text contains forbidden wallet send/sign keywords."""
    if _WALLET_SEND_KEYWORDS.search(text):
        raise ValueError("text must not contain wallet send or signing keywords")


def normalize_wallet_name(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = str(value).strip()
    return trimmed or None


def normalize_txid(value: object) -> str | None:
    if value is None:
        return None
    trimmed = str(value).strip()
    return trimmed or None


def normalize_amount(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    amount = Decimal(str(value))
    if amount < 0:
        amount = -amount
    return amount


def _to_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return bool(value)


def _unix_to_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def infer_maturity_status(
    wallet_event: Mapping[str, Any],
    *,
    maturity_confirmations: int = DEFAULT_MATURITY_CONFIRMATIONS,
) -> str:
    """Derive maturity_status from a wallet listtransactions row."""
    if _to_bool(wallet_event.get("abandoned")):
        return "abandoned"

    confirmations = wallet_event.get("confirmations")
    try:
        conf_int = int(confirmations) if confirmations is not None else 0
    except (TypeError, ValueError):
        conf_int = 0
    if conf_int < 0:
        return "conflicted"

    category = str(wallet_event.get("category") or "").strip().lower()
    if category == "immature":
        return "immature"
    if category == "orphan":
        return "orphaned"
    if category == "generate":
        if conf_int >= maturity_confirmations:
            return "mature"
        return "immature"
    return "unknown"


def wallet_event_to_reward_event(
    wallet_event: Mapping[str, Any],
    *,
    wallet_name: str | None = None,
    maturity_confirmations: int = DEFAULT_MATURITY_CONFIRMATIONS,
) -> SupportWalletRewardEvent | None:
    """Normalize a listtransactions row into a reward event, or None if ignored."""
    category = str(wallet_event.get("category") or "").strip().lower()
    if category in IGNORED_WALLET_CATEGORIES:
        return None
    if category not in REWARD_EVENT_CATEGORIES:
        return None

    txid = normalize_txid(wallet_event.get("txid"))
    if not txid:
        return None

    try:
        confirmations = int(wallet_event.get("confirmations", 0))
    except (TypeError, ValueError):
        confirmations = 0

    vout_raw = wallet_event.get("vout")
    vout: int | None
    try:
        vout = int(vout_raw) if vout_raw is not None else None
    except (TypeError, ValueError):
        vout = None

    blockheight_raw = wallet_event.get("blockheight")
    blockheight: int | None
    try:
        blockheight = int(blockheight_raw) if blockheight_raw is not None else None
    except (TypeError, ValueError):
        blockheight = None

    blockindex_raw = wallet_event.get("blockindex")
    blockindex: int | None
    try:
        blockindex = int(blockindex_raw) if blockindex_raw is not None else None
    except (TypeError, ValueError):
        blockindex = None

    maturity_status = infer_maturity_status(
        wallet_event, maturity_confirmations=maturity_confirmations
    )
    if maturity_status not in VALID_REWARD_MATURITY_STATUSES:
        maturity_status = "unknown"

    raw_event = dict(wallet_event)
    return SupportWalletRewardEvent(
        wallet_name=normalize_wallet_name(wallet_name),
        txid=txid,
        vout=vout,
        category=category or None,
        amount=normalize_amount(wallet_event.get("amount")),
        confirmations=max(confirmations, 0),
        blockhash=wallet_event.get("blockhash"),
        blockheight=blockheight,
        blockindex=blockindex,
        blocktime=_unix_to_datetime(wallet_event.get("blocktime")),
        event_time=_unix_to_datetime(wallet_event.get("time")),
        trusted=_to_bool(wallet_event.get("trusted")),
        spendable=_to_bool(wallet_event.get("spendable")),
        generated=_to_bool(wallet_event.get("generated"))
        if wallet_event.get("generated") is not None
        else category in {"generate", "immature"},
        immature=_to_bool(wallet_event.get("immature"))
        if wallet_event.get("immature") is not None
        else category == "immature"
        or (
            category == "generate"
            and confirmations < maturity_confirmations
        ),
        abandoned=_to_bool(wallet_event.get("abandoned")),
        maturity_status=maturity_status,
        raw_wallet_event=raw_event,
    )


def build_reward_events_sql(
    *,
    include_raw: bool = False,
    maturity_status: str | None = None,
) -> str:
    raw_column = ", raw_wallet_event" if include_raw else ""
    where_clause = ""
    if maturity_status is not None:
        status = str(maturity_status).strip()
        if status not in VALID_REWARD_MATURITY_STATUSES:
            raise ValueError(f"invalid maturity_status filter: {status}")
        where_clause = f"\nWHERE maturity_status = '{status}'"
    sql = f"""
SELECT
  id,
  wallet_name,
  txid,
  vout,
  category,
  amount,
  confirmations,
  blockhash,
  blockheight,
  blockindex,
  blocktime,
  event_time,
  trusted,
  spendable,
  generated,
  immature,
  abandoned,
  maturity_status,
  first_seen_at,
  last_seen_at,
  updated_at{raw_column}
FROM support_wallet_reward_events{where_clause}
ORDER BY last_seen_at DESC, id DESC
""".strip()
    if _READONLY_SQL_FORBIDDEN.search(sql):
        raise ValueError("SQL must be read-only SELECT only")
    return sql


def build_upsert_reward_event_sql() -> str:
    sql = """
INSERT INTO support_wallet_reward_events (
  wallet_name,
  txid,
  vout,
  category,
  amount,
  confirmations,
  blockhash,
  blockheight,
  blockindex,
  blocktime,
  event_time,
  trusted,
  spendable,
  generated,
  immature,
  abandoned,
  maturity_status,
  raw_wallet_event
) VALUES (
  %(wallet_name)s,
  %(txid)s,
  %(vout)s,
  %(category)s,
  %(amount)s,
  %(confirmations)s,
  %(blockhash)s,
  %(blockheight)s,
  %(blockindex)s,
  %(blocktime)s,
  %(event_time)s,
  %(trusted)s,
  %(spendable)s,
  %(generated)s,
  %(immature)s,
  %(abandoned)s,
  %(maturity_status)s,
  %(raw_wallet_event)s::jsonb
)
ON CONFLICT (wallet_name, txid, vout) DO UPDATE SET
  category = EXCLUDED.category,
  amount = EXCLUDED.amount,
  confirmations = EXCLUDED.confirmations,
  blockhash = EXCLUDED.blockhash,
  blockheight = EXCLUDED.blockheight,
  blockindex = EXCLUDED.blockindex,
  blocktime = EXCLUDED.blocktime,
  event_time = EXCLUDED.event_time,
  trusted = EXCLUDED.trusted,
  spendable = EXCLUDED.spendable,
  generated = EXCLUDED.generated,
  immature = EXCLUDED.immature,
  abandoned = EXCLUDED.abandoned,
  maturity_status = EXCLUDED.maturity_status,
  raw_wallet_event = EXCLUDED.raw_wallet_event,
  last_seen_at = now(),
  updated_at = now()
""".strip()
    assert_no_wallet_send_keywords(sql)
    if "support_wallet_reward_events" not in sql:
        raise ValueError("upsert must target support_wallet_reward_events only")
    return sql


def reward_event_to_upsert_params(event: SupportWalletRewardEvent) -> dict[str, Any]:
    return {
        "wallet_name": event.wallet_name,
        "txid": event.txid,
        "vout": event.vout,
        "category": event.category,
        "amount": event.amount,
        "confirmations": event.confirmations,
        "blockhash": event.blockhash,
        "blockheight": event.blockheight,
        "blockindex": event.blockindex,
        "blocktime": event.blocktime,
        "event_time": event.event_time,
        "trusted": event.trusted,
        "spendable": event.spendable,
        "generated": event.generated,
        "immature": event.immature,
        "abandoned": event.abandoned,
        "maturity_status": event.maturity_status,
        "raw_wallet_event": json.dumps(event.raw_wallet_event),
    }


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _serialize_amount(value: object) -> str:
    if value is None:
        return "0"
    return format(Decimal(str(value)), "f")


def _to_int(value: object) -> int:
    if value is None:
        return 0
    return int(value)


def row_to_reward_event_dict(
    row: Mapping[str, Any],
    *,
    include_raw: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": _to_int(row.get("id")) if row.get("id") is not None else None,
        "wallet_name": row.get("wallet_name"),
        "txid": str(row["txid"]),
        "vout": row.get("vout"),
        "category": row.get("category"),
        "amount": _serialize_amount(row.get("amount")),
        "confirmations": _to_int(row.get("confirmations")),
        "blockhash": row.get("blockhash"),
        "blockheight": row.get("blockheight"),
        "blockindex": row.get("blockindex"),
        "blocktime": _serialize_datetime(row.get("blocktime")),
        "event_time": _serialize_datetime(row.get("event_time")),
        "trusted": row.get("trusted"),
        "spendable": row.get("spendable"),
        "generated": row.get("generated"),
        "immature": row.get("immature"),
        "abandoned": row.get("abandoned"),
        "maturity_status": row.get("maturity_status"),
        "first_seen_at": _serialize_datetime(row.get("first_seen_at")),
        "last_seen_at": _serialize_datetime(row.get("last_seen_at")),
        "updated_at": _serialize_datetime(row.get("updated_at")),
    }
    if include_raw:
        raw = row.get("raw_wallet_event")
        if isinstance(raw, str):
            result["raw_wallet_event"] = json.loads(raw)
        elif raw is None:
            result["raw_wallet_event"] = {}
        else:
            result["raw_wallet_event"] = dict(raw)
    return result


def reward_event_to_dict(
    event: SupportWalletRewardEvent,
    *,
    include_raw: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "wallet_name": event.wallet_name,
        "txid": event.txid,
        "vout": event.vout,
        "category": event.category,
        "amount": _serialize_amount(event.amount),
        "confirmations": event.confirmations,
        "blockhash": event.blockhash,
        "blockheight": event.blockheight,
        "blockindex": event.blockindex,
        "blocktime": _serialize_datetime(event.blocktime),
        "event_time": _serialize_datetime(event.event_time),
        "trusted": event.trusted,
        "spendable": event.spendable,
        "generated": event.generated,
        "immature": event.immature,
        "abandoned": event.abandoned,
        "maturity_status": event.maturity_status,
    }
    if include_raw:
        result["raw_wallet_event"] = event.raw_wallet_event
    return result
