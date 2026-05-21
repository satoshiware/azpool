from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

from payouts.collector.app import admin_readonly


def test_assert_readonly_sql_accepts_select() -> None:
    admin_readonly.assert_readonly_sql("SELECT 1 FROM pool_instances")


@pytest.mark.parametrize(
    "keyword",
    [
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "DROP",
        "ALTER",
        "CREATE",
        "GRANT",
        "REVOKE",
        "VACUUM",
        "CALL",
    ],
)
def test_assert_readonly_sql_rejects_mutating_keywords(keyword: str) -> None:
    with pytest.raises(ValueError, match="read-only"):
        admin_readonly.assert_readonly_sql(f"SELECT 1; {keyword} INTO pool_instances VALUES (1)")


def test_pool_instances_sql_references_table() -> None:
    sql = admin_readonly.build_pool_instances_sql()
    assert "FROM pool_instances" in sql
    admin_readonly.assert_readonly_sql(sql)


def test_sc_nodes_sql_references_table() -> None:
    sql = admin_readonly.build_sc_nodes_sql()
    assert "FROM sc_nodes" in sql
    admin_readonly.assert_readonly_sql(sql)


def test_mappings_sql_joins_sc_nodes() -> None:
    sql = admin_readonly.build_identity_mappings_sql()
    assert "FROM sc_node_identity_mappings" in sql
    assert "LEFT JOIN sc_nodes" in sql
    admin_readonly.assert_readonly_sql(sql)


def test_payout_addresses_sql_joins_sc_nodes() -> None:
    sql = admin_readonly.build_payout_addresses_sql()
    assert "sc_node_payout_addresses" in sql
    assert "LEFT JOIN sc_nodes" in sql
    admin_readonly.assert_readonly_sql(sql)


def test_admin_command_map_includes_payout_addresses() -> None:
    from payouts.scripts import pool_ledger_admin_readonly as admin_cli

    assert "payout-addresses" in admin_cli._COMMANDS
    build_sql, row_fn = admin_cli._COMMANDS["payout-addresses"]
    assert build_sql() == admin_readonly.build_payout_addresses_sql()
    assert row_fn is admin_readonly.row_to_payout_address_dict


def test_reward_events_sql_is_select_only() -> None:
    sql = admin_readonly.build_reward_events_sql()
    assert "support_wallet_reward_events" in sql
    assert "raw_wallet_event" not in sql
    admin_readonly.assert_readonly_sql(sql)


def test_admin_command_map_includes_reward_events() -> None:
    from payouts.scripts import pool_ledger_admin_readonly as admin_cli

    assert "reward-events" in admin_cli._COMMANDS
    build_sql, row_fn = admin_cli._COMMANDS["reward-events"]
    assert build_sql(None) == admin_readonly.build_reward_events_sql()
    assert row_fn is admin_readonly.row_to_reward_event_dict


def test_reward_event_dict_hides_raw_wallet_event() -> None:
    result = admin_readonly.row_to_reward_event_dict(
        {
            "id": 1,
            "txid": "abc",
            "amount": Decimal("1"),
            "confirmations": 2,
            "maturity_status": "mature",
            "raw_wallet_event": {"txid": "abc"},
        }
    )
    assert "raw_wallet_event" not in result


def test_unmapped_identities_sql_filters_null_sc_node_id() -> None:
    sql = admin_readonly.build_unmapped_identities_sql(limit=20)
    assert "sc_node_id IS NULL" in sql
    assert "LIMIT 20" in sql
    admin_readonly.assert_readonly_sql(sql)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, 1),
        (1, 1),
        (500, 500),
        (501, 500),
        (-5, 1),
        (9999, 500),
    ],
)
def test_unmapped_limit_is_clamped(raw: int, expected: int) -> None:
    assert admin_readonly.clamp_unmapped_limit(raw) == expected
    sql = admin_readonly.build_unmapped_identities_sql(limit=raw)
    assert f"LIMIT {expected}" in sql


def test_row_dicts_do_not_expose_secrets() -> None:
    forbidden_keys = {"password", "secret", "database_url", "DATABASE_URL", "token", "api_key"}
    pool_row = {
        "id": "pool01",
        "display_name": "Pool 01",
        "monitoring_base_url": "http://10.10.70.131:9090",
        "status": "active",
        "monitoring_enabled": True,
        "created_at": None,
        "updated_at": None,
    }
    sc_row = {
        "id": "sc-2",
        "display_name": "SC Node 2",
        "status": "active",
        "payout_enabled": False,
        "created_at": None,
        "updated_at": None,
    }
    mapping_row = {
        "id": 1,
        "sc_node_id": "sc-2",
        "sc_node_display_name": "SC Node 2",
        "match_type": "prefix",
        "match_value": "baveetstudy.",
        "status": "active",
        "created_at": None,
    }
    unmapped_row = {
        "user_identity": "baveetstudy.miner1",
        "delta_rows": 3,
        "accepted_delta_total": Decimal("1"),
        "work_delta_total": Decimal("2"),
        "first_observed_at": None,
        "last_observed_at": None,
    }

    for result in (
        admin_readonly.row_to_pool_instance_dict(pool_row),
        admin_readonly.row_to_sc_node_dict(sc_row),
        admin_readonly.row_to_identity_mapping_dict(mapping_row),
        admin_readonly.row_to_unmapped_identity_dict(unmapped_row),
    ):
        assert forbidden_keys.isdisjoint(result.keys())
        for value in result.values():
            if isinstance(value, str):
                assert "postgresql://" not in value


def test_bool_flags_serialize_as_bool() -> None:
    pool = admin_readonly.row_to_pool_instance_dict(
        {
            "id": "pool01",
            "display_name": "Pool 01",
            "monitoring_base_url": "http://example",
            "status": "active",
            "monitoring_enabled": 1,
            "created_at": None,
            "updated_at": None,
        }
    )
    sc = admin_readonly.row_to_sc_node_dict(
        {
            "id": "sc-2",
            "display_name": "SC Node 2",
            "status": "active",
            "payout_enabled": 0,
            "created_at": None,
            "updated_at": None,
        }
    )
    assert pool["monitoring_enabled"] is True
    assert isinstance(pool["monitoring_enabled"], bool)
    assert sc["payout_enabled"] is False
    assert isinstance(sc["payout_enabled"], bool)


def test_decimal_and_datetime_serialization() -> None:
    observed = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
    result = admin_readonly.row_to_unmapped_identity_dict(
        {
            "user_identity": "baveetstudy.miner1",
            "delta_rows": 2,
            "accepted_delta_total": Decimal("1.5"),
            "work_delta_total": Decimal("9"),
            "first_observed_at": observed,
            "last_observed_at": observed,
        }
    )
    assert result["accepted_delta_total"] == "1.5"
    assert result["work_delta_total"] == "9"
    assert isinstance(result["accepted_delta_total"], str)
    assert result["first_observed_at"] == observed.isoformat()
    assert result["last_observed_at"] == observed.isoformat()
