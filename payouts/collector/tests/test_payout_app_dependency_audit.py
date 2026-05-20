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

from payouts.scripts import audit_payout_app_dependencies as audit


def test_high_risk_modules_classify_high_risk() -> None:
    for name in (
        "main.py",
        "settlement.py",
        "postgres_settlement.py",
        "sender.py",
        "postgres_sender.py",
        "reward_contract.py",
        "db.py",
        "init_db.py",
        "models.py",
    ):
        status = audit.suggest_status(f"payouts/app/{name}", deploy_referenced=False)
        assert status == "LEGACY-CANDIDATE-HIGH-RISK"


def test_supporting_modules_classify_supporting() -> None:
    for name in ("poller.py", "postgres_shadow_compare.py", "scheduler.py"):
        status = audit.suggest_status(f"payouts/app/{name}", deploy_referenced=False)
        assert status == "LEGACY-CANDIDATE-SUPPORTING"


def test_deploy_reference_appends_do_not_remove_yet() -> None:
    status = audit.suggest_status("payouts/app/main.py", deploy_referenced=True)
    assert status == "LEGACY-CANDIDATE-HIGH-RISK; DO-NOT-REMOVE-YET"


def test_deploy_reference_detection_ignores_collector_main() -> None:
    deploy_text = "ExecStart=python -m payouts.collector.app.main"
    assert audit._deploy_references("payouts/app/main.py", deploy_text) is False


def test_deploy_reference_detection_finds_uvicorn() -> None:
    deploy_text = "uvicorn app.main:app --reload"
    assert audit._deploy_references("payouts/app/main.py", deploy_text) is True


@pytest.mark.parametrize(
    "path",
    [
        "payouts/collector/app/__pycache__/main.cpython-312.pyc",
        "payouts/tests/.pytest_cache/v/cache/nodeids",
        "payouts/scripts/foo.pyo",
        "payouts/lib.so",
        ".venv/lib/python3.12/site-packages/x.py",
        "payouts/.git/config",
    ],
)
def test_ignored_audit_paths(path: str) -> None:
    assert audit._is_ignored_audit_path(path) is True


def test_source_paths_are_not_ignored() -> None:
    assert audit._is_ignored_audit_path("payouts/tests/test_settlement.py") is False
    assert audit._is_ignored_audit_path("payouts/scripts/run_translator_sv1_capture_proxy.py") is False


def test_inbound_references_ignore_cache_files() -> None:
    search_files = [
        ("payouts/tests/test_settlement.py", "from app.db import make_engine"),
        (
            "payouts/collector/app/__pycache__/db.cpython-312.pyc",
            "from app.db import make_engine",
        ),
    ]
    count, examples = audit._inbound_references("payouts/app/db.py", search_files)
    assert count == 1
    assert examples == ["payouts/tests/test_settlement.py"]
    assert not any("__pycache__" in example for example in examples)
    assert not any(example.endswith(".pyc") for example in examples)


def test_audit_output_excludes_cache_from_inbound_examples() -> None:
    payload = audit.audit_app_dependencies(AZPOOL_ROOT)
    for module in payload["modules"]:
        for example in module.get("inbound_reference_examples", []):
            assert "__pycache__" not in example
            assert not str(example).endswith(".pyc")
            assert ".pytest_cache" not in example


def test_audit_output_metadata_only() -> None:
    payload = audit.audit_app_dependencies(AZPOOL_ROOT)
    assert "modules" in payload
    assert "summary" in payload
    forbidden = {"content", "contents", "body", "secret", "password", "database_url"}
    for module in payload["modules"]:
        assert forbidden.isdisjoint(module.keys())
        assert "path" in module
        assert module["path"].startswith("payouts/app/")
        assert "suggested_status" in module
        for value in module.values():
            if isinstance(value, str):
                assert "postgresql://" not in value


def test_broken_pipe_exits_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_broken_pipe(*_args: object, **_kwargs: object) -> None:
        raise BrokenPipeError()

    monkeypatch.setattr(audit, "audit_app_dependencies", lambda *_a, **_k: {"modules": []})
    monkeypatch.setattr(audit.json, "dump", _raise_broken_pipe)
    assert audit.main() == 0


def test_script_cli_produces_json_without_db() -> None:
    result = subprocess.run(
        [sys.executable, str(AZPOOL_ROOT / "payouts/scripts/audit_payout_app_dependencies.py")],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(AZPOOL_ROOT),
        env={**os.environ, "PYTHONPATH": str(AZPOOL_ROOT)},
    )
    payload = json.loads(result.stdout)
    assert payload["summary"]["app_python_modules"] >= 1
