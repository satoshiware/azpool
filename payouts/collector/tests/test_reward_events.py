from __future__ import annotations

import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

from payouts.collector.app import reward_events


_MUTATING_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE|VACUUM|CALL)\b",
    re.IGNORECASE,
)

_WALLET_SEND_KEYWORDS = re.compile(
    r"\b("
    r"sendmany|sendtoaddress|sendrawtransaction|walletpassphrase|"
    r"createrawtransaction|createwallet|loadwallet|dumpprivkey|"
    r"signrawtransaction|privkey"
    r")\b",
    re.IGNORECASE,
)


def _base_event(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "txid": "abc123",
        "category": "generate",
        "amount": 1.25,
        "confirmations": 10,
        "time": 1_700_000_000,
    }
    payload.update(overrides)
    return payload


def test_receive_send_move_ignored() -> None:
    for category in ("receive", "send", "move"):
        assert (
            reward_events.wallet_event_to_reward_event(_base_event(category=category))
            is None
        )


def test_immature_category_maps_immature_status() -> None:
    event = reward_events.wallet_event_to_reward_event(
        _base_event(category="immature", confirmations=0)
    )
    assert event is not None
    assert event.maturity_status == "immature"


def test_orphan_category_maps_orphaned_status() -> None:
    event = reward_events.wallet_event_to_reward_event(
        _base_event(category="orphan", confirmations=0)
    )
    assert event is not None
    assert event.maturity_status == "orphaned"


def test_negative_confirmations_maps_conflicted() -> None:
    event = reward_events.wallet_event_to_reward_event(
        _base_event(category="generate", confirmations=-1)
    )
    assert event is not None
    assert event.maturity_status == "conflicted"


def test_abandoned_maps_abandoned_status() -> None:
    event = reward_events.wallet_event_to_reward_event(
        _base_event(category="generate", abandoned=True, confirmations=5)
    )
    assert event is not None
    assert event.maturity_status == "abandoned"


def test_generate_mature_by_confirmations() -> None:
    event = reward_events.wallet_event_to_reward_event(
        _base_event(category="generate", confirmations=100),
        maturity_confirmations=100,
    )
    assert event is not None
    assert event.maturity_status == "mature"


def test_generate_immature_below_threshold() -> None:
    event = reward_events.wallet_event_to_reward_event(
        _base_event(category="generate", confirmations=10),
        maturity_confirmations=100,
    )
    assert event is not None
    assert event.maturity_status == "immature"


def test_positive_amount_normalization() -> None:
    event = reward_events.wallet_event_to_reward_event(_base_event(amount=-2.5))
    assert event is not None
    assert event.amount == Decimal("2.5")


def test_missing_txid_returns_none() -> None:
    assert reward_events.wallet_event_to_reward_event(_base_event(txid="")) is None
    assert reward_events.wallet_event_to_reward_event(_base_event(txid="   ")) is None
    assert reward_events.wallet_event_to_reward_event(_base_event(txid=None)) is None


def test_build_reward_events_sql_is_select_only() -> None:
    sql = reward_events.build_reward_events_sql(include_raw=False)
    assert "FROM support_wallet_reward_events" in sql
    assert "raw_wallet_event" not in sql
    assert _MUTATING_SQL.search(sql) is None


def test_build_reward_events_sql_can_include_raw() -> None:
    sql = reward_events.build_reward_events_sql(include_raw=True)
    assert "raw_wallet_event" in sql
    assert _MUTATING_SQL.search(sql) is None


def test_build_reward_events_sql_maturity_filter() -> None:
    sql = reward_events.build_reward_events_sql(maturity_status="mature")
    assert "maturity_status = 'mature'" in sql


def test_build_upsert_reward_event_sql_only_touches_reward_events_table() -> None:
    sql = reward_events.build_upsert_reward_event_sql()
    assert "support_wallet_reward_events" in sql
    assert "INSERT INTO support_wallet_reward_events" in sql
    assert "pool_instances" not in sql
    assert "sc_node_payout_addresses" not in sql


def test_row_to_reward_event_dict_hides_raw_by_default() -> None:
    result = reward_events.row_to_reward_event_dict(
        {
            "id": 1,
            "wallet_name": "SUPPORT",
            "txid": "abc",
            "vout": 0,
            "category": "generate",
            "amount": Decimal("1"),
            "confirmations": 1,
            "maturity_status": "immature",
            "raw_wallet_event": {"txid": "abc", "secret": "nope"},
        }
    )
    assert "raw_wallet_event" not in result


def test_row_to_reward_event_dict_includes_raw_when_requested() -> None:
    result = reward_events.row_to_reward_event_dict(
        {
            "id": 1,
            "txid": "abc",
            "amount": Decimal("1"),
            "confirmations": 1,
            "maturity_status": "immature",
            "raw_wallet_event": {"txid": "abc"},
        },
        include_raw=True,
    )
    assert result["raw_wallet_event"] == {"txid": "abc"}


def test_implementation_files_do_not_introduce_wallet_send_keywords() -> None:
    guard_block = re.compile(
        r"_WALLET_SEND_KEYWORDS = re\.compile\([\s\S]*?\)\n",
        re.MULTILINE,
    )
    for rel in (
        "payouts/collector/app/reward_events.py",
        "payouts/scripts/support_wallet_reward_events.py",
    ):
        text = (AZPOOL_ROOT / rel).read_text(encoding="utf-8")
        scrubbed = guard_block.sub("", text, count=1)
        assert _WALLET_SEND_KEYWORDS.search(scrubbed) is None


def test_assert_no_wallet_send_keywords_rejects_forbidden_terms() -> None:
    with pytest.raises(ValueError):
        reward_events.assert_no_wallet_send_keywords("sendtoaddress")


def test_script_listtransactions_uses_arg_list_without_shell() -> None:
    from payouts.scripts import support_wallet_reward_events as script

    argv = script._listtransactions_argv(azc_bin="azc", wallet="SUPPORT", count=50)
    assert argv == ["azc", "-rpcwallet=SUPPORT", "listtransactions", "*", "50", "0"]
    assert isinstance(argv, list)


def test_script_subprocess_run_does_not_use_shell() -> None:
    source = (
        AZPOOL_ROOT / "payouts/scripts/support_wallet_reward_events.py"
    ).read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "subprocess.run" in source
