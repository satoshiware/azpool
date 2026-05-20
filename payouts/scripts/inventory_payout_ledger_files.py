#!/usr/bin/env python3
"""Read-only filesystem inventory helper for payout-ledger legacy cleanup."""

from __future__ import annotations

import json
import sys
from pathlib import Path

CLASSIFICATIONS = frozenset(
    {"ACTIVE", "LEGACY-CANDIDATE", "UNKNOWN", "DO-NOT-REMOVE-YET"}
)

KEYWORDS = (
    "settlement",
    "payout",
    "sender",
    "sqlite",
    "wallet",
    "sendtoaddress",
    "sendmany",
    "sendrawtransaction",
    "user_identity",
    "sc_node_id",
    "pool_share_work_deltas",
    "pool_instances",
    "sc_node_identity_mappings",
)

_ACTIVE_EXACT = frozenset(
    {
        "payouts/scripts/sc_node_work_summary.py",
        "payouts/scripts/pool_ledger_admin_readonly.py",
        "payouts/scripts/inventory_payout_ledger_files.py",
        "payouts/migrations/001_pool_telemetry_collector.sql",
        "payouts/migrations/002_sc_node_identity_mapping.sql",
        "payouts/migrations/003_pool_instance_registry.sql",
        "docs/runbooks/pool-monitoring-collector.md",
        "docs/runbooks/pool-ledger-admin.md",
        "docs/adr/ADR-support-node-pool-telemetry-collector.md",
        "docs/adr/ADR-pool-ledger-legacy-cleanup-plan.md",
        "docs/inventory/payout-ledger-file-inventory.md",
    }
)

_ACTIVE_PREFIXES = (
    "payouts/collector/app/",
    "payouts/collector/tests/",
)

_LEGACY_PREFIXES = (
    "payouts/app/",
)

_LEGACY_EXACT = frozenset(
    {
        "payouts/scripts/demo_interval_run.py",
        "payouts/scripts/backfill_postgres_shadow.py",
        "payouts/scripts/backfill_sqlite_settlement_mapping.py",
    }
)

_LEGACY_TEST_PREFIX = "payouts/tests/"

_DO_NOT_REMOVE_PREFIXES = (
    "payouts/migrations/",
    "payouts/alembic/",
)

_DO_NOT_REMOVE_EXACT = frozenset(
    {
        "payouts/alembic.ini",
        "payouts/alembic/env.py",
    }
)

_SCAN_ROOTS = (
    "payouts/app",
    "payouts/collector/app",
    "payouts/scripts",
    "payouts/tests",
    "payouts/collector/tests",
    "payouts/migrations",
    "payouts/alembic",
    "docs/runbooks",
    "docs/adr",
    "docs/inventory",
)


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/")


def suggest_classification(path: str) -> str:
    """Conservative suggested classification for a repo-relative file path."""
    normalized = _normalize_path(path)
    if normalized in _ACTIVE_EXACT:
        return "ACTIVE"
    for prefix in _ACTIVE_PREFIXES:
        if normalized.startswith(prefix):
            return "ACTIVE"
    if normalized in _LEGACY_EXACT:
        return "LEGACY-CANDIDATE"
    for prefix in _LEGACY_PREFIXES:
        if normalized.startswith(prefix):
            return "LEGACY-CANDIDATE"
    if normalized.startswith(_LEGACY_TEST_PREFIX):
        return "LEGACY-CANDIDATE"
    if normalized in _DO_NOT_REMOVE_EXACT:
        return "DO-NOT-REMOVE-YET"
    for prefix in _DO_NOT_REMOVE_PREFIXES:
        if normalized.startswith(prefix):
            return "DO-NOT-REMOVE-YET"
    return "UNKNOWN"


def _find_keywords(text: str) -> list[str]:
    lowered = text.lower()
    return [keyword for keyword in KEYWORDS if keyword in lowered]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def scan_inventory(repo_root: Path | None = None) -> list[dict[str, object]]:
    root = repo_root or _repo_root()
    entries: list[dict[str, object]] = []

    for scan_root in _SCAN_ROOTS:
        base = root / scan_root
        if not base.exists():
            continue
        for file_path in sorted(base.rglob("*")):
            if not file_path.is_file():
                continue
            rel = _normalize_path(str(file_path.relative_to(root)))
            try:
                raw = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                raw = ""
            entries.append(
                {
                    "path": rel,
                    "extension": file_path.suffix or "",
                    "size_bytes": file_path.stat().st_size,
                    "contains_keywords": _find_keywords(raw),
                    "suggested_classification": suggest_classification(rel),
                }
            )

    entries.sort(key=lambda item: str(item["path"]))
    return entries


def _write_json_payload(payload: dict[str, object]) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def main() -> int:
    payload = {
        "classifications": sorted(CLASSIFICATIONS),
        "files": scan_inventory(),
    }
    try:
        _write_json_payload(payload)
    except BrokenPipeError:
        # Pipe consumer (e.g. head) closed stdout early; not an inventory failure.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
