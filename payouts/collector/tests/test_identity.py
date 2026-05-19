from __future__ import annotations

import sys
from pathlib import Path

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

import pytest

from payouts.collector.app.identity import parse_sc_node_id


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        ("az/scnode/sc-node-42", "sc-node-42"),
        ("scnode.node99", "node99"),
        ("scnode-sc-west-1", "sc-west-1"),
    ],
)
def test_parse_sc_node_id_mapped_identities(identity: str, expected: str) -> None:
    assert parse_sc_node_id(identity) == expected


@pytest.mark.parametrize(
    "identity",
    [
        "baveetstudy.miner1",
        "baveetstudy.miner2",
        "baveetstudy.miner3",
        "baveet.miner1",
        "",
        "   ",
    ],
)
def test_parse_sc_node_id_unknown_identities_stay_unmapped(identity: str) -> None:
    assert parse_sc_node_id(identity) is None
