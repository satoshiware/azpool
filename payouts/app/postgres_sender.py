from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from app.postgres_repositories import PostgresLedgerRepository
from app.sender import SenderStats


def _to_btc_str(value: Any) -> str:
    return f"{Decimal(str(value)):.8f}"


def _build_payload_postgres(
    settlement_credit: dict[str, Any],
) -> dict[str, Any]:
    """Build payout event payload from Postgres settlement credit."""
    return {
        "settlement_id": int(settlement_credit["settlement_id"]),
        "period_start": settlement_credit["work_window_start"].isoformat(),
        "period_end": settlement_credit["work_window_end"].isoformat(),
        "user_id": int(settlement_credit["user_id"]),
        "username": settlement_credit["username"],
        "credit_id": int(settlement_credit["settlement_credit_id"]),
        "amount_btc": _to_btc_str(Decimal(str(settlement_credit["amount_sats"])) / Decimal("100000000")),
        "idempotency_key": settlement_credit["idempotency_key"],
    }


def _get_or_create_payout_event_postgres(
    repository: PostgresLedgerRepository,
    settlement_credit_id: int,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Get existing or create new payout event."""
    event = repository.get_payout_event_by_settlement_credit_id(settlement_credit_id)
    if event is not None:
        return event, False

    event = repository.create_payout_event(
        settlement_credit_id=settlement_credit_id,
        payload_json=json.dumps(payload, sort_keys=True),
        status="pending",
    )
    return event, True


def process_payout_events_postgres(
    repository: PostgresLedgerRepository,
    *,
    dry_run: bool = True,
    transport: Callable[[dict[str, Any], bool], bool] = None,
) -> SenderStats:
    """Create missing events and send unsent payout events exactly once via Postgres."""
    if transport is None:
        from app.sender import send_payout_event
        transport = send_payout_event

    pending_events = repository.list_pending_payout_events()

    attempted = 0
    sent = 0
    failed = 0
    created_events = 0

    for event_row in pending_events:
        credit_id = int(event_row["settlement_credit_id"])
        payload = _build_payload_postgres(event_row)
        event, created = _get_or_create_payout_event_postgres(repository, credit_id, payload)
        if created:
            created_events += 1

        if event["status"] == "sent":
            # Already sent, update credit status to match
            repository.update_settlement_credit_status(credit_id, "sent")
            continue

        attempted += 1
        ok = transport(payload, dry_run)
        if ok:
            repository.update_payout_event_status(credit_id, "sent")
            repository.update_settlement_credit_status(credit_id, "sent")
            sent += 1
        else:
            repository.update_payout_event_status(credit_id, "pending")
            failed += 1

    return SenderStats(
        attempted=attempted,
        sent=sent,
        failed=failed,
        created_events=created_events,
    )
