"""
Unit tests for Phase 2 logic.

These tests cover the new Postgres detail/metrics payload builders
without importing the FastAPI app.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.postgres_read_payloads import (
    build_latest_settlement_payload,
    build_service_metrics_payload,
)


class TestLatestSettlementPayload:
    def test_empty_detail_returns_empty_shape(self):
        payload = build_latest_settlement_payload(None)

        assert payload == {"settlement": None, "users": []}

    def test_detail_payload_formats_settlement_and_users(self):
        detail = {
            "settlement": {
                "id": 42,
                "status": "completed",
                "work_window_start": datetime(2024, 1, 1, 10, 0, 0, tzinfo=UTC),
                "work_window_end": datetime(2024, 1, 1, 11, 0, 0, tzinfo=UTC),
                "total_reward_sats": 125000000,
                "total_shares": 300,
                "total_work": Decimal("12.5"),
            },
            "user_credits": [
                {
                    "username": "miner1",
                    "amount_sats": 50000,
                    "status": "sent",
                },
            ],
            "user_work": [
                {
                    "username": "miner1",
                    "work_delta": Decimal("5.0"),
                    "share_delta": 10,
                    "payout_fraction": Decimal("0.25"),
                },
            ],
        }

        payload = build_latest_settlement_payload(detail)

        assert payload["settlement"]["settlement_id"] == 42
        assert payload["settlement"]["period_start"] == "2024-01-01T10:00:00+00:00"
        assert payload["settlement"]["period_end"] == "2024-01-01T11:00:00+00:00"
        assert payload["settlement"]["pool_reward_btc"] == "1.25000000"
        assert payload["settlement"]["total_shares"] == 300
        assert payload["settlement"]["total_work"] == "12.50000000"
        assert payload["users"][0]["username"] == "miner1"
        assert payload["users"][0]["amount_btc"] == "0.00050000"
        assert payload["users"][0]["contribution_value"] == "5.00000000"
        assert payload["users"][0]["payout_fraction"] == "0.25"
        assert payload["users"][0]["status"] == "sent"


class TestServiceMetricsPayload:
    def test_metrics_payload_formats_timestamp(self):
        summary = {
            "settlements_total": 11,
            "payouts_sent_total": 7,
            "payout_failures_total": 2,
            "last_settlement_timestamp": datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
        }

        payload = build_service_metrics_payload(summary)

        assert payload == {
            "settlements_total": 11,
            "payouts_sent_total": 7,
            "payout_failures_total": 2,
            "last_settlement_timestamp": "2024-01-01T12:00:00+00:00",
        }

    def test_metrics_payload_handles_none_timestamp(self):
        payload = build_service_metrics_payload(
            {
                "settlements_total": 0,
                "payouts_sent_total": 0,
                "payout_failures_total": 0,
                "last_settlement_timestamp": None,
            }
        )

        assert payload["settlements_total"] == 0
        assert payload["last_settlement_timestamp"] is None
