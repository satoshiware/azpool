from __future__ import annotations

import re

# Canonical SC-node identity: az/scnode/<sc_node_id>
_SCNODE_SLASH = re.compile(r"^az/scnode/([^/]+)$")

# Documented legacy aliases (explicit, tested only):
#   scnode.<sc_node_id>
#   scnode-<sc_node_id>
_SCNODE_DOT = re.compile(r"^scnode\.([A-Za-z0-9._-]+)$")
_SCNODE_DASH = re.compile(r"^scnode-([A-Za-z0-9._-]+)$")


def parse_sc_node_id(user_identity: str) -> str | None:
    """Derive sc_node_id from user_identity when a safe, documented rule matches.

    Unknown or unmapped identities (for example baveetstudy.miner1) return None.
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
