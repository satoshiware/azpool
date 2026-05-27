from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

GUARD_SCRIPT = AZPOOL_ROOT / "deploy/wallet-wrappers/azc-payout-readonly-guard.sh"


def _run_guard(
    argv: list[str],
    *,
    azcoin_cli: Path,
    conf: Path,
    datadir: Path,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "AZCOIN_CLI": str(azcoin_cli),
        "AZCOIN_CONF": str(conf),
        "AZCOIN_DATADIR": str(datadir),
    }
    return subprocess.run(
        ["bash", str(GUARD_SCRIPT), *argv],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def mock_azcoin_cli(tmp_path: Path) -> Path:
    script = tmp_path / "mock-azcoin-cli"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if printf "%s\\n" "$@" | grep -qx listunspent || [[ " $* " == *" listunspent "* ]]; then\n'
        "  echo '[]'\n"
        'elif printf "%s\\n" "$@" | grep -qx getbalances || [[ " $* " == *" getbalances "* ]]; then\n'
        '  echo \'{"mine":{"trusted":1,"immature":0}}\'\n'
        "else\n"
        '  echo "unexpected: $*" >&2\n'
        "  exit 9\n"
        "fi\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_guard_source_lists_listunspent_in_help_and_deny_messages() -> None:
    text = GUARD_SCRIPT.read_text(encoding="utf-8")
    assert "listunspent" in text
    assert (
        "only getbalances, gettransaction, listtransactions, and listunspent are allowed"
        in text
    )
    assert (
        "expected -rpcwallet=wallet getbalances|gettransaction|listtransactions|listunspent"
        in text
    )
    assert "sendtoaddress" not in text
    assert "walletpassphrase" not in text


def test_guard_rejects_sendtoaddress(mock_azcoin_cli: Path, tmp_path: Path) -> None:
    completed = _run_guard(
        ["-rpcwallet=wallet", "sendtoaddress", "az1test", "1.0"],
        azcoin_cli=mock_azcoin_cli,
        conf=tmp_path / "azcoin.conf",
        datadir=tmp_path / "datadir",
    )
    assert completed.returncode == 2
    assert "only getbalances, gettransaction, listtransactions, and listunspent" in (
        completed.stderr
    )


def test_guard_allows_listunspent_with_minconf(
    mock_azcoin_cli: Path,
    tmp_path: Path,
) -> None:
    completed = _run_guard(
        ["-rpcwallet=wallet", "listunspent", "1"],
        azcoin_cli=mock_azcoin_cli,
        conf=tmp_path / "azcoin.conf",
        datadir=tmp_path / "datadir",
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout) == []


def test_guard_rejects_non_wallet_rpcwallet(mock_azcoin_cli: Path, tmp_path: Path) -> None:
    completed = _run_guard(
        ["-rpcwallet=SUPPORT", "listunspent", "1"],
        azcoin_cli=mock_azcoin_cli,
        conf=tmp_path / "azcoin.conf",
        datadir=tmp_path / "datadir",
    )
    assert completed.returncode == 2
    assert "only -rpcwallet=wallet is allowed" in completed.stderr
