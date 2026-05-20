from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

from payouts.collector.app import sc_node_summary


_MUTATING_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE)\b",
    re.IGNORECASE,
)


def _assert_read_only_sql(sql: str) -> None:
    assert _MUTATING_SQL.search(sql) is None


def test_sc_node_sql_requires_mapped_sc_node_id() -> None:
    sql = sc_node_summary.build_sc_node_summary_sql()
    assert "sc_node_id IS NOT NULL" in sql
    _assert_read_only_sql(sql)


def test_unmapped_sql_requires_null_sc_node_id() -> None:
    sql = sc_node_summary.build_unmapped_summary_sql()
    assert "sc_node_id IS NULL" in sql
    _assert_read_only_sql(sql)


def test_sc_node_dict_excludes_user_identity() -> None:
    row = {
        "sc_node_id": "sc-3",
        "display_name": "SC Node 3",
        "status": "active",
        "payout_enabled": True,
        "accepted_delta_total": Decimal("10"),
        "work_delta_total": Decimal("100.5"),
        "delta_rows": 4,
        "first_observed_at": datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
        "last_observed_at": datetime(2026, 5, 19, 13, 0, tzinfo=timezone.utc),
    }
    result = sc_node_summary.row_to_sc_node_summary_dict(row)
    assert "user_identity" not in result
    assert result["payout_enabled"] is True
    assert result["accepted_delta_total"] == "10"
    assert result["work_delta_total"] == "100.5"


def test_unmapped_dict_excludes_user_identity() -> None:
    row = {
        "accepted_delta_total": Decimal("3"),
        "work_delta_total": Decimal("30"),
        "delta_rows": 2,
        "first_observed_at": None,
        "last_observed_at": None,
    }
    result = sc_node_summary.row_to_unmapped_summary_dict(row)
    assert "user_identity" not in result
    assert result["accepted_delta_total"] == "3"
    assert result["work_delta_total"] == "30"


def test_payout_enabled_serializes_as_boolean() -> None:
    for raw, expected in ((True, True), (False, False), (None, False), (1, True), (0, False)):
        row = {
            "sc_node_id": "sc-1",
            "display_name": "Node 1",
            "status": "active",
            "payout_enabled": raw,
            "accepted_delta_total": Decimal("0"),
            "work_delta_total": Decimal("0"),
            "delta_rows": 0,
            "first_observed_at": None,
            "last_observed_at": None,
        }
        result = sc_node_summary.row_to_sc_node_summary_dict(row)
        assert result["payout_enabled"] is expected
        assert isinstance(result["payout_enabled"], bool)


def test_numeric_totals_serialize_as_strings() -> None:
    sc_row = {
        "sc_node_id": "sc-2",
        "display_name": None,
        "status": None,
        "payout_enabled": False,
        "accepted_delta_total": Decimal("1.25"),
        "work_delta_total": Decimal("9"),
        "delta_rows": 1,
        "first_observed_at": None,
        "last_observed_at": None,
    }
    unmapped_row = {
        "accepted_delta_total": Decimal("0.5"),
        "work_delta_total": Decimal("2"),
        "delta_rows": 1,
        "first_observed_at": None,
        "last_observed_at": None,
    }

    sc_result = sc_node_summary.row_to_sc_node_summary_dict(sc_row)
    unmapped_result = sc_node_summary.row_to_unmapped_summary_dict(unmapped_row)

    assert isinstance(sc_result["accepted_delta_total"], str)
    assert isinstance(sc_result["work_delta_total"], str)
    assert isinstance(unmapped_result["accepted_delta_total"], str)
    assert isinstance(unmapped_result["work_delta_total"], str)
    assert sc_result["accepted_delta_total"] == "1.25"
    assert sc_result["work_delta_total"] == "9"
    assert unmapped_result["accepted_delta_total"] == "0.5"
    assert unmapped_result["work_delta_total"] == "2"
