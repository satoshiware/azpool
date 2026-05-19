from __future__ import annotations

import sys
from pathlib import Path

AZPOOL_ROOT = Path(__file__).resolve().parents[3]
if str(AZPOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(AZPOOL_ROOT))

import pytest

from payouts.collector.app import main as collector_main
from payouts.collector.app.config import CollectorSettings, PoolInstanceConfig


def _settings() -> CollectorSettings:
    return CollectorSettings(
        database_url="postgresql://example",
        env_pool_instances=(
            PoolInstanceConfig(id="pool01", base_url="http://10.10.70.131:9090"),
        ),
    )


def test_collect_once_skips_when_lock_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Conn:
        pass

    def _connect(_database_url: str):
        class _Ctx:
            def __enter__(self):
                return _Conn()

            def __exit__(self, *_args):
                return False

        return _Ctx()

    monkeypatch.setattr(collector_main, "connect", _connect)
    monkeypatch.setattr(collector_main, "try_acquire_collector_lock", lambda _conn: False)
    monkeypatch.setattr(
        collector_main,
        "start_collector_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not start run")),
    )

    totals = collector_main.collect_once(_settings())

    assert totals == collector_main._empty_totals()


def test_collect_once_runs_when_lock_acquired(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"release": 0, "start": 0}

    class _Conn:
        pass

    conn = _Conn()

    def _connect(_database_url: str):
        class _Ctx:
            def __enter__(self):
                return conn

            def __exit__(self, *_args):
                return False

        return _Ctx()

    def _start_collector_run(_conn):
        calls["start"] += 1
        return 42

    def _release(_conn):
        calls["release"] += 1
        return True

    monkeypatch.setattr(collector_main, "connect", _connect)
    monkeypatch.setattr(collector_main, "try_acquire_collector_lock", lambda _conn: True)
    monkeypatch.setattr(collector_main, "release_collector_lock", _release)
    monkeypatch.setattr(collector_main, "fetch_active_pool_instances", lambda _conn: ())
    monkeypatch.setattr(collector_main, "resolve_pool_instances", lambda _db, env: env)
    monkeypatch.setattr(collector_main, "start_collector_run", _start_collector_run)
    monkeypatch.setattr(collector_main, "fetch_active_identity_mappings", lambda _conn: [])
    monkeypatch.setattr(collector_main, "finish_collector_run", lambda *_args, **_kwargs: None)

    class _Txn:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    conn.transaction = lambda: _Txn()
    conn.commit = lambda: None

    monkeypatch.setattr(collector_main, "ensure_pool_instance", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(collector_main, "fetch_health", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(collector_main, "fetch_clients", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(collector_main, "fetch_client_channels", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(collector_main, "normalize_channels", lambda *_args, **_kwargs: [])

    totals = collector_main.collect_once(_settings())

    assert calls["start"] == 1
    assert calls["release"] == 1
    assert totals["pools_checked"] == 1


def test_collect_once_releases_lock_after_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    released = {"value": False}

    class _Conn:
        def transaction(self):
            raise RuntimeError("boom")

        def commit(self):
            return None

    conn = _Conn()

    def _connect(_database_url: str):
        class _Ctx:
            def __enter__(self):
                return conn

            def __exit__(self, *_args):
                return False

        return _Ctx()

    monkeypatch.setattr(collector_main, "connect", _connect)
    monkeypatch.setattr(collector_main, "try_acquire_collector_lock", lambda _conn: True)
    monkeypatch.setattr(collector_main, "release_collector_lock", lambda _conn: released.update(value=True) or True)
    monkeypatch.setattr(collector_main, "fetch_active_pool_instances", lambda _conn: ())
    monkeypatch.setattr(collector_main, "resolve_pool_instances", lambda _db, env: env)
    monkeypatch.setattr(collector_main, "start_collector_run", lambda _conn: 7)
    monkeypatch.setattr(collector_main, "fetch_active_identity_mappings", lambda _conn: [])
    monkeypatch.setattr(collector_main, "_finish_failed_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(collector_main, "ensure_pool_instance", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(collector_main, "fetch_health", lambda *_args, **_kwargs: {})

    with pytest.raises(RuntimeError, match="boom"):
        collector_main.collect_once(_settings())

    assert released["value"] is True


def test_main_exits_zero_when_lock_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collector_main, "load_settings", _settings)
    monkeypatch.setattr(collector_main, "collect_once", lambda _settings: collector_main._empty_totals())

    assert collector_main.main() == 0


def test_collect_once_pre_run_row_failure_skips_finish_failed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released = {"value": False}
    finish_called = {"value": False}

    class _Conn:
        pass

    conn = _Conn()

    def _connect(_database_url: str):
        class _Ctx:
            def __enter__(self):
                return conn

            def __exit__(self, *_args):
                return False

        return _Ctx()

    monkeypatch.setattr(collector_main, "connect", _connect)
    monkeypatch.setattr(collector_main, "try_acquire_collector_lock", lambda _conn: True)
    monkeypatch.setattr(collector_main, "release_collector_lock", lambda _conn: released.update(value=True) or True)
    monkeypatch.setattr(
        collector_main,
        "fetch_active_pool_instances",
        lambda _conn: (_ for _ in ()).throw(RuntimeError("registry boom")),
    )
    monkeypatch.setattr(
        collector_main,
        "_finish_failed_run",
        lambda *_args, **_kwargs: finish_called.update(value=True),
    )

    with pytest.raises(RuntimeError, match="registry boom"):
        collector_main.collect_once(_settings())

    assert finish_called["value"] is False
    assert released["value"] is True
