from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any


def _to_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _to_decimal_str(value: object) -> str:
    return f"{Decimal(str(value or 0)):.8f}"


def _sats_to_btc_str(value: object) -> str:
    return _to_decimal_str(Decimal(str(_to_int(value))) / Decimal("100000000"))


def build_latest_settlement_payload(detail: dict[str, Any] | None) -> dict[str, Any]:
    if not detail:
        return {"settlement": None, "users": []}

    settlement = detail.get("settlement") or {}
    credit_rows = list(detail.get("user_credits") or [])
    user_work_rows = list(detail.get("user_work") or [])
    work_by_username = {str(work.get("username") or ""): work for work in user_work_rows}

    return {
        "settlement": {
            "settlement_id": _to_int(settlement.get("id")),
            "status": settlement.get("status"),
            "period_start": settlement.get("work_window_start").isoformat()
            if settlement.get("work_window_start")
            else None,
            "period_end": settlement.get("work_window_end").isoformat()
            if settlement.get("work_window_end")
            else None,
            "pool_reward_btc": _sats_to_btc_str(settlement.get("total_reward_sats")),
            "total_shares": _to_int(settlement.get("total_shares")),
            "total_work": _to_decimal_str(settlement.get("total_work") or "0"),
        },
        "users": [
            {
                "username": str(credit.get("username") or ""),
                "contribution_value": _to_decimal_str(
                    (
                        work_by_username.get(str(credit.get("username") or ""), {}).get("work_delta")
                        if Decimal(
                            str(work_by_username.get(str(credit.get("username") or ""), {}).get("work_delta") or "0")
                        )
                        > 0
                        else work_by_username.get(str(credit.get("username") or ""), {}).get("share_delta", 0)
                    )
                    or "0"
                ),
                "payout_fraction": str(
                    work_by_username.get(str(credit.get("username") or ""), {}).get("payout_fraction") or "0"
                ),
                "amount_btc": _sats_to_btc_str(credit.get("amount_sats")),
                "status": credit.get("status"),
            }
            for credit in credit_rows
        ],
    }


def build_service_metrics_payload(summary: dict[str, Any]) -> dict[str, Any]:
    last_settlement_timestamp = summary.get("last_settlement_timestamp")
    return {
        "settlements_total": int(summary.get("settlements_total") or 0),
        "payouts_sent_total": int(summary.get("payouts_sent_total") or 0),
        "payout_failures_total": int(summary.get("payout_failures_total") or 0),
        "last_settlement_timestamp": last_settlement_timestamp.isoformat()
        if isinstance(last_settlement_timestamp, datetime)
        else last_settlement_timestamp,
    }