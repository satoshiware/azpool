#!/usr/bin/env python3
"""Read-only dependency audit for payouts/app (no imports of app modules)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

KEYWORDS = (
    "FastAPI",
    "settlement",
    "payout",
    "sender",
    "wallet",
    "sqlite",
    "postgres",
    "UserPayout",
    "Settlement",
    "sendtoaddress",
    "sendmany",
    "sendrawtransaction",
)

HIGH_RISK_MODULES = frozenset(
    {
        "main.py",
        "settlement.py",
        "postgres_settlement.py",
        "sender.py",
        "postgres_sender.py",
        "reward_contract.py",
        "db.py",
        "init_db.py",
        "models.py",
    }
)

SEARCH_ROOTS = (
    "payouts/tests",
    "payouts/collector",
    "payouts/scripts",
    "payouts/legacy",
    "docs",
    "deploy",
)

MAX_REFERENCE_EXAMPLES = 5

_IGNORED_PATH_PARTS = frozenset({"__pycache__", ".pytest_cache", ".git", ".venv"})
_IGNORED_SUFFIXES = (".pyc", ".pyo", ".so")


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/")


def _is_ignored_audit_path(rel_path: str) -> bool:
    """Return True for generated/cache paths excluded from inbound reference counts."""
    normalized = _normalize_path(rel_path)
    if normalized.endswith(_IGNORED_SUFFIXES):
        return True
    return bool(_IGNORED_PATH_PARTS.intersection(normalized.split("/")))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _find_keywords(text: str) -> list[str]:
    hits: list[str] = []
    for keyword in KEYWORDS:
        if keyword in text or keyword.lower() in text.lower():
            hits.append(keyword)
    return hits


def _module_stem(app_rel_path: str) -> str:
    return Path(app_rel_path).stem


def _reference_patterns(app_rel_path: str) -> list[str]:
    stem = _module_stem(app_rel_path)
    filename = Path(app_rel_path).name
    patterns = [
        f"from app.{stem}",
        f"import app.{stem}",
        f"app/{filename}",
        f"payouts/app/{filename}",
    ]
    if stem == "main":
        patterns.extend(["app.main", "uvicorn app.main", "from app import main"])
    return patterns


def _collect_search_files(root: Path) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    candidates: list[Path] = []
    for search_root in SEARCH_ROOTS:
        base = root / search_root
        if base.exists():
            candidates.extend(base.rglob("*"))
    readme = root / "README.md"
    if readme.is_file():
        candidates.append(readme)
    payouts_readme = root / "payouts" / "README.md"
    if payouts_readme.is_file():
        candidates.append(payouts_readme)

    seen: set[str] = set()
    for file_path in sorted(candidates):
        if not file_path.is_file():
            continue
        rel = _normalize_path(str(file_path.relative_to(root)))
        if rel.startswith("payouts/app/"):
            continue
        if _is_ignored_audit_path(rel):
            continue
        if rel in seen:
            continue
        seen.add(rel)
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        files.append((rel, text))
    return files


def _deploy_text(root: Path) -> str:
    deploy = root / "deploy"
    if not deploy.exists():
        return ""
    chunks: list[str] = []
    for file_path in deploy.rglob("*"):
        if not file_path.is_file():
            continue
        rel = _normalize_path(str(file_path.relative_to(root)))
        if _is_ignored_audit_path(rel):
            continue
        try:
            chunks.append(file_path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(chunks)


def _inbound_references(app_rel_path: str, search_files: list[tuple[str, str]]) -> tuple[int, list[str]]:
    patterns = _reference_patterns(app_rel_path)
    examples: list[str] = []
    count = 0
    for rel, text in search_files:
        if _is_ignored_audit_path(rel):
            continue
        matched = any(pattern in text for pattern in patterns)
        if not matched:
            continue
        count += 1
        if len(examples) < MAX_REFERENCE_EXAMPLES:
            examples.append(rel)
    return count, examples


def _deploy_references(app_rel_path: str, deploy_text: str) -> bool:
    if not deploy_text:
        return False
    filename = Path(app_rel_path).name
    stem = _module_stem(app_rel_path)
    if f"payouts/app/{filename}" in deploy_text:
        return True
    if f"payouts.app.{stem}" in deploy_text:
        return True
    if stem == "main" and (
        "uvicorn app.main" in deploy_text or "app.main:app" in deploy_text
    ):
        return True
    return False


def suggest_status(app_rel_path: str, *, deploy_referenced: bool) -> str:
    filename = Path(app_rel_path).name
    if filename in HIGH_RISK_MODULES:
        base = "LEGACY-CANDIDATE-HIGH-RISK"
    else:
        base = "LEGACY-CANDIDATE-SUPPORTING"
    if deploy_referenced:
        return f"{base}; DO-NOT-REMOVE-YET"
    return base


def audit_app_dependencies(repo_root: Path | None = None) -> dict[str, object]:
    root = repo_root or _repo_root()
    app_dir = root / "payouts" / "app"
    search_files = _collect_search_files(root)
    deploy_text = _deploy_text(root)

    modules: list[dict[str, object]] = []
    status_counts: dict[str, int] = {}

    for file_path in sorted(app_dir.glob("*.py")):
        rel = _normalize_path(str(file_path.relative_to(root)))
        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        inbound_count, inbound_examples = _inbound_references(rel, search_files)
        deploy_ref = _deploy_references(rel, deploy_text)
        status = suggest_status(rel, deploy_referenced=deploy_ref)
        status_counts[status] = status_counts.get(status, 0) + 1
        modules.append(
            {
                "path": rel,
                "size_bytes": file_path.stat().st_size,
                "keyword_hits": _find_keywords(text),
                "inbound_reference_count": inbound_count,
                "inbound_reference_examples": inbound_examples,
                "deploy_or_systemd_referenced": deploy_ref,
                "suggested_status": status,
            }
        )

    return {
        "summary": {
            "app_python_modules": len(modules),
            "search_roots_scanned": list(SEARCH_ROOTS),
            "ignored_path_rules": {
                "path_parts": sorted(_IGNORED_PATH_PARTS),
                "suffixes": list(_IGNORED_SUFFIXES),
            },
            "status_counts": dict(sorted(status_counts.items())),
            "deploy_references_payouts_app": "payouts/app" in deploy_text or "payouts.app" in deploy_text,
            "collector_references_payouts_app": any(
                ("from app." in text or "import app." in text)
                for rel, text in search_files
                if rel.startswith("payouts/collector/") and not _is_ignored_audit_path(rel)
            ),
            "active_script_dependencies": [
                "payouts/scripts/run_translator_sv1_capture_proxy.py",
            ],
        },
        "modules": modules,
    }


def _write_json_payload(payload: dict[str, object]) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def main() -> int:
    try:
        _write_json_payload(audit_app_dependencies())
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
