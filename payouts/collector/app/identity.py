from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Sequence

# Canonical SC-node identity: az/scnode/<sc_node_id>
_SCNODE_SLASH = re.compile(r"^az/scnode/([^/]+)$")

# Documented legacy aliases (explicit, tested only):
#   scnode.<sc_node_id>
#   scnode-<sc_node_id>
_SCNODE_DOT = re.compile(r"^scnode\.([A-Za-z0-9._-]+)$")
_SCNODE_DASH = re.compile(r"^scnode-([A-Za-z0-9._-]+)$")


@dataclass(frozen=True)
class IdentityMapping:
    id: int
    sc_node_id: str
    match_type: str
    match_value: str


def parse_sc_node_id(user_identity: str) -> str | None:
    """Parse native SC-node identity formats only.

    Unknown identities (for example baveetstudy.miner1) return None unless
    resolved later via database mappings.
    """
    value = (user_identity or "").strip()
    if not value:
        return None

    for pattern in (_SCNODE_SLASH, _SCNODE_DOT, _SCNODE_DASH):
        match = pattern.match(value)
        if match:
            sc_node_id = match.group(1).strip()
            return sc_node_id or None

    return None


def resolve_sc_node_id(
    user_identity: str,
    mappings: Sequence[IdentityMapping],
) -> str | None:
    """Resolve sc_node_id from native format first, then active DB mappings."""
    value = (user_identity or "").strip()
    if not value:
        return None

    native = parse_sc_node_id(value)
    if native is not None:
        return native

    if not mappings:
        return None

    exact_matches = [
        mapping
        for mapping in mappings
        if mapping.match_type == "exact" and mapping.match_value == value
    ]
    if exact_matches:
        return sorted(exact_matches, key=lambda mapping: mapping.id)[0].sc_node_id

    prefix_matches = [
        mapping
        for mapping in mappings
        if mapping.match_type == "prefix" and value.startswith(mapping.match_value)
    ]
    if prefix_matches:
        best = max(prefix_matches, key=lambda mapping: (len(mapping.match_value), mapping.id))
        return best.sc_node_id

    glob_matches = [
        mapping
        for mapping in mappings
        if mapping.match_type == "glob" and fnmatch.fnmatchcase(value, mapping.match_value)
    ]
    if glob_matches:
        best = sorted(glob_matches, key=lambda mapping: (mapping.match_value, mapping.id))[0]
        return best.sc_node_id

    return None
