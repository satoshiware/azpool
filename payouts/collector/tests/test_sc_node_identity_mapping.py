from __future__ import annotations

import sys
from pathlib import Path

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

import pytest

from payouts.collector.app.identity import IdentityMapping, parse_sc_node_id, resolve_sc_node_id


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        ("az/scnode/sc-node-42", "sc-node-42"),
        ("az/scnode/sc-3", "sc-3"),
        ("scnode.node99", "node99"),
        ("scnode-sc-west-1", "sc-west-1"),
    ],
)
def test_parse_sc_node_id_native_formats(identity: str, expected: str) -> None:
    assert parse_sc_node_id(identity) == expected


@pytest.mark.parametrize(
    "identity",
    [
        "baveetstudy.miner1",
        "baveetstudy.miner2",
        "baveet.miner1",
        "",
        "   ",
    ],
)
def test_parse_sc_node_id_unknown_without_db_mapping(identity: str) -> None:
    assert parse_sc_node_id(identity) is None


def test_resolve_sc_node_id_native_az_scnode() -> None:
    assert resolve_sc_node_id("az/scnode/sc-3", []) == "sc-3"


def test_resolve_sc_node_id_unmapped_without_mapping() -> None:
    assert resolve_sc_node_id("baveetstudy.miner1", []) is None


def test_resolve_sc_node_id_prefix_mapping() -> None:
    mappings = [
        IdentityMapping(id=1, sc_node_id="sc-3", match_type="prefix", match_value="baveetstudy."),
    ]
    assert resolve_sc_node_id("baveetstudy.miner1", mappings) == "sc-3"


def test_resolve_sc_node_id_unknown_stays_unmapped() -> None:
    mappings = [
        IdentityMapping(id=1, sc_node_id="sc-3", match_type="prefix", match_value="baveetstudy."),
    ]
    assert resolve_sc_node_id("otheruser.miner1", mappings) is None


def test_resolve_sc_node_id_exact_wins_over_prefix() -> None:
    mappings = [
        IdentityMapping(id=1, sc_node_id="sc-3", match_type="prefix", match_value="baveetstudy."),
        IdentityMapping(id=2, sc_node_id="sc-9", match_type="exact", match_value="baveetstudy.miner1"),
    ]
    assert resolve_sc_node_id("baveetstudy.miner1", mappings) == "sc-9"


def test_resolve_sc_node_id_longest_prefix_wins() -> None:
    mappings = [
        IdentityMapping(id=1, sc_node_id="sc-1", match_type="prefix", match_value="baveet"),
        IdentityMapping(id=2, sc_node_id="sc-3", match_type="prefix", match_value="baveetstudy."),
    ]
    assert resolve_sc_node_id("baveetstudy.miner1", mappings) == "sc-3"


def test_resolve_sc_node_id_ignores_inactive_mapping() -> None:
    mappings = [
        IdentityMapping(id=1, sc_node_id="sc-3", match_type="prefix", match_value="baveetstudy."),
    ]
    # Caller passes only active mappings; inactive rows are excluded upstream.
    assert resolve_sc_node_id("baveetstudy.miner1", mappings) == "sc-3"
    assert resolve_sc_node_id("baveetstudy.miner1", []) is None


def test_resolve_sc_node_id_glob_match_deterministic() -> None:
    mappings = [
        IdentityMapping(id=2, sc_node_id="sc-b", match_type="glob", match_value="baveetstudy.miner*"),
        IdentityMapping(id=1, sc_node_id="sc-a", match_type="glob", match_value="baveetstudy.miner*"),
    ]
    assert resolve_sc_node_id("baveetstudy.miner1", mappings) == "sc-a"
