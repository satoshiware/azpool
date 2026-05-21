from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

from payouts.collector.app import payout_addresses


_MUTATING_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE|VACUUM|CALL)\b",
    re.IGNORECASE,
)

_WALLET_KEYWORDS = re.compile(
    r"sendmany|sendtoaddress|sendrawtransaction|walletpassphrase|createrawtransaction",
    re.IGNORECASE,
)


def _assert_readonly_sql(sql: str) -> None:
    assert _MUTATING_SQL.search(sql) is None
    payout_addresses.assert_readonly_sql(sql)


def test_valid_status_and_source_constants() -> None:
    assert payout_addresses.VALID_PAYOUT_ADDRESS_STATUSES == frozenset(
        {"pending_verification", "active", "inactive", "revoked"}
    )
    assert payout_addresses.VALID_PAYOUT_ADDRESS_SOURCES == frozenset(
        {"manual", "imported", "wallet", "api"}
    )


def test_normalize_payout_address_trims_whitespace() -> None:
    assert payout_addresses.normalize_payout_address("  addr1  ") == "addr1"


def test_empty_payout_address_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        payout_addresses.validate_payout_address_record(
            sc_node_id="sc-2",
            payout_address="   ",
            status="pending_verification",
            address_source="manual",
        )


def test_invalid_status_rejected() -> None:
    with pytest.raises(ValueError, match="invalid status"):
        payout_addresses.validate_payout_address_record(
            sc_node_id="sc-2",
            payout_address="addr1",
            status="bogus",
            address_source="manual",
        )


def test_invalid_source_rejected() -> None:
    with pytest.raises(ValueError, match="invalid address_source"):
        payout_addresses.validate_payout_address_record(
            sc_node_id="sc-2",
            payout_address="addr1",
            status="pending_verification",
            address_source="bogus",
        )


def test_build_manual_register_record_defaults() -> None:
    record = payout_addresses.build_manual_register_record(
        sc_node_id="sc-2",
        payout_address="  addr1  ",
        label="primary",
    )
    assert record == {
        "sc_node_id": "sc-2",
        "payout_address": "addr1",
        "status": "pending_verification",
        "address_source": "manual",
        "is_default": False,
        "label": "primary",
    }


def test_build_manual_register_record_rejects_invalid_default() -> None:
    with pytest.raises(ValueError, match="is_default requires status active"):
        payout_addresses.build_manual_register_record(
            sc_node_id="sc-2",
            payout_address="addr1",
            is_default=True,
            status="pending_verification",
        )


def test_migration_enforces_one_active_default_per_sc_node() -> None:
    migration = (AZPOOL_ROOT / "payouts/migrations/004_sc_node_payout_addresses.sql").read_text(
        encoding="utf-8"
    )
    assert "idx_sc_node_payout_addresses_one_active_default" in migration
    assert "is_default = true AND status = 'active'" in migration
    assert "UNIQUE" in migration


def test_migration_includes_retired_at_column() -> None:
    migration_004 = (AZPOOL_ROOT / "payouts/migrations/004_sc_node_payout_addresses.sql").read_text(
        encoding="utf-8"
    )
    migration_005 = (AZPOOL_ROOT / "payouts/migrations/005_sc_node_payout_addresses_retired_at.sql").read_text(
        encoding="utf-8"
    )
    assert "retired_at TIMESTAMPTZ" in migration_004
    assert "ADD COLUMN IF NOT EXISTS retired_at" in migration_005


def test_default_requires_active_status() -> None:
    with pytest.raises(ValueError, match="is_default requires status active"):
        payout_addresses.validate_payout_address_record(
            sc_node_id="sc-2",
            payout_address="addr1",
            status="pending_verification",
            address_source="manual",
            is_default=True,
        )


def test_build_sc_node_payout_addresses_sql_is_select_only() -> None:
    sql = payout_addresses.build_sc_node_payout_addresses_sql(include_inactive=True)
    assert "retired_at" in sql
    assert "sc_node_payout_addresses" in sql
    assert "LEFT JOIN sc_nodes" in sql
    _assert_readonly_sql(sql)


def test_build_sc_node_payout_addresses_sql_can_exclude_inactive() -> None:
    sql = payout_addresses.build_sc_node_payout_addresses_sql(include_inactive=False)
    assert "pending_verification" in sql
    assert "active" in sql
    _assert_readonly_sql(sql)


def test_build_active_default_payout_addresses_sql() -> None:
    sql = payout_addresses.build_active_default_payout_addresses_sql()
    assert "is_default = true" in sql
    assert "status = 'active'" in sql
    assert "sc_node_payout_addresses" in sql
    _assert_readonly_sql(sql)


def test_row_to_payout_address_dict_serializes_types() -> None:
    observed = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
    result = payout_addresses.row_to_payout_address_dict(
        {
            "id": 1,
            "sc_node_id": "sc-2",
            "sc_node_display_name": "SC Node 2",
            "payout_address": "addr1",
            "label": "primary",
            "address_source": "manual",
            "status": "active",
            "is_default": 1,
            "verified_at": observed,
            "retired_at": None,
            "created_at": observed,
            "updated_at": observed,
        }
    )
    assert result["retired_at"] is None
    assert result["is_default"] is True
    assert isinstance(result["is_default"], bool)
    assert result["verified_at"] == observed.isoformat()
    assert result["created_at"] == observed.isoformat()
    assert result["updated_at"] == observed.isoformat()


def test_implementation_files_do_not_introduce_wallet_keywords() -> None:
    for rel in (
        "payouts/collector/app/payout_addresses.py",
        "payouts/collector/app/admin_readonly.py",
    ):
        text = (AZPOOL_ROOT / rel).read_text(encoding="utf-8")
        assert _WALLET_KEYWORDS.search(text) is None
