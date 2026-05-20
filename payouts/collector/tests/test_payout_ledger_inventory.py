from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

from payouts.scripts import inventory_payout_ledger_files as inventory


def test_classification_enum_values() -> None:
    assert inventory.CLASSIFICATIONS == frozenset(
        {"ACTIVE", "LEGACY-CANDIDATE", "UNKNOWN", "DO-NOT-REMOVE-YET"}
    )


def test_known_active_collector_paths() -> None:
    assert inventory.suggest_classification("payouts/collector/app/main.py") == "ACTIVE"
    assert inventory.suggest_classification("payouts/collector/tests/test_delta.py") == "ACTIVE"


def test_known_active_scripts() -> None:
    assert inventory.suggest_classification("payouts/scripts/sc_node_work_summary.py") == "ACTIVE"
    assert inventory.suggest_classification("payouts/scripts/pool_ledger_admin_readonly.py") == "ACTIVE"


def test_legacy_settlement_and_sender() -> None:
    assert inventory.suggest_classification("payouts/app/settlement.py") == "LEGACY-CANDIDATE"
    assert inventory.suggest_classification("payouts/app/sender.py") == "LEGACY-CANDIDATE"


def test_alembic_versions_do_not_remove_yet() -> None:
    assert (
        inventory.suggest_classification(
            "payouts/alembic/versions/20260504_0001_create_payout_ledger_postgres_schema.py"
        )
        == "DO-NOT-REMOVE-YET"
    )


def test_unknown_paths() -> None:
    assert inventory.suggest_classification("payouts/plan/db_wiring.md") == "UNKNOWN"
    assert inventory.suggest_classification("payouts/README.md") == "UNKNOWN"


def test_quarantined_legacy_scripts_classify_legacy_candidate() -> None:
    assert (
        inventory.suggest_classification("payouts/legacy/scripts/demo_interval_run.py")
        == "LEGACY-CANDIDATE"
    )
    assert (
        inventory.suggest_classification("payouts/legacy/scripts/backfill_postgres_shadow.py")
        == "LEGACY-CANDIDATE"
    )
    assert (
        inventory.suggest_classification(
            "payouts/legacy/scripts/backfill_sqlite_settlement_mapping.py"
        )
        == "LEGACY-CANDIDATE"
    )


def test_legacy_readme_classifies_do_not_remove_yet() -> None:
    assert inventory.suggest_classification("payouts/legacy/README.md") == "DO-NOT-REMOVE-YET"
    assert inventory.suggest_classification("payouts/app/README.md") == "DO-NOT-REMOVE-YET"


def test_scan_roots_include_payouts_legacy() -> None:
    assert "payouts/legacy" in inventory._SCAN_ROOTS


def test_broken_pipe_on_stdout_exits_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_broken_pipe(*_args: object, **_kwargs: object) -> None:
        raise BrokenPipeError()

    monkeypatch.setattr(inventory, "scan_inventory", lambda: [])
    monkeypatch.setattr(inventory.json, "dump", _raise_broken_pipe)

    assert inventory.main() == 0


def test_script_output_shape_excludes_contents_and_secrets() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(AZPOOL_ROOT / "payouts/scripts/inventory_payout_ledger_files.py"),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(AZPOOL_ROOT),
        env={**os.environ, "PYTHONPATH": str(AZPOOL_ROOT)},
    )
    payload = json.loads(result.stdout)
    assert "files" in payload
    assert payload["classifications"] == sorted(inventory.CLASSIFICATIONS)

    forbidden_keys = {"content", "contents", "body", "secret", "password", "database_url"}
    for entry in payload["files"]:
        assert forbidden_keys.isdisjoint(entry.keys())
        assert "path" in entry
        assert "suggested_classification" in entry
        assert entry["suggested_classification"] in inventory.CLASSIFICATIONS
        for value in entry.values():
            if isinstance(value, str):
                assert "postgresql://" not in value
