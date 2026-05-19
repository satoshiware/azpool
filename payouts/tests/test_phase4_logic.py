"""
Unit tests for Phase 4 block-flow migration logic.

All tests are pure-logic and run without a real Postgres connection or FastAPI.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.runtime_cutover import should_fail_closed_on_postgres_primary


# ---------------------------------------------------------------------------
# Helper: simulate the Postgres-first block read/reward/link decision
# ---------------------------------------------------------------------------

def _simulate_block_read_mode(
    *,
    postgres_primary_session_enabled: bool,
    pg_repo_available: bool,
) -> str:
    """
    Mirrors the _use_pg_blocks branching logic added to _execute_settlement_cycle.

    Returns 'postgres' if Postgres path is taken, 'sqlite' if SQLite fallback.
    """
    if postgres_primary_session_enabled:
        if pg_repo_available:
            return "postgres"
        else:
            # repo unavailable → fail closed when primary enabled
            if should_fail_closed_on_postgres_primary(
                postgres_primary_session_enabled=True,
                sqlite_retirement_mode_enabled=False,
            ):
                return "error"
    return "sqlite"


def test_block_read_uses_postgres_when_primary_enabled() -> None:
    result = _simulate_block_read_mode(
        postgres_primary_session_enabled=True,
        pg_repo_available=True,
    )
    assert result == "postgres"


def test_block_read_uses_sqlite_when_primary_disabled() -> None:
    result = _simulate_block_read_mode(
        postgres_primary_session_enabled=False,
        pg_repo_available=False,
    )
    assert result == "sqlite"


def test_block_read_errors_when_primary_enabled_and_repo_unavailable() -> None:
    result = _simulate_block_read_mode(
        postgres_primary_session_enabled=True,
        pg_repo_available=False,
    )
    assert result == "error"


# ---------------------------------------------------------------------------
# Reward deduplication logic
# ---------------------------------------------------------------------------

def _dedup_block_rows(
    matured: list[dict],
    retry: list[dict],
) -> dict[str, dict]:
    """Mirrors the rows_by_hash deduplication in the Postgres path."""
    rows_by_hash: dict[str, dict] = {}
    for row in matured:
        rows_by_hash[str(row["blockhash"])] = dict(row)
    for row in retry:
        bh = str(row["blockhash"])
        if bh not in rows_by_hash:
            rows_by_hash[bh] = dict(row)
    return rows_by_hash


def test_matured_blocks_take_precedence_over_retry() -> None:
    matured = [{"blockhash": "aaa", "found_at": "2025-01-01", "reward_sats": None}]
    retry = [{"blockhash": "aaa", "found_at": "2025-01-01", "reward_sats": 0}]
    deduped = _dedup_block_rows(matured, retry)
    assert len(deduped) == 1
    assert deduped["aaa"]["reward_sats"] is None  # matured row preserved


def test_retry_only_blocks_added_when_not_in_matured() -> None:
    matured = [{"blockhash": "aaa", "found_at": "2025-01-01", "reward_sats": 1000}]
    retry = [
        {"blockhash": "bbb", "found_at": "2024-12-01", "reward_sats": 0},
    ]
    deduped = _dedup_block_rows(matured, retry)
    assert set(deduped.keys()) == {"aaa", "bbb"}


# ---------------------------------------------------------------------------
# Reward computation logic (mirrors settlement cycle total_sats calculation)
# ---------------------------------------------------------------------------

def _compute_settlement_reward(
    selected_rows: list[dict],
    rewards_sats_by_hash: dict[str, int],
    matured_hashes: list[str],
) -> tuple[Decimal, bool]:
    """
    Mirrors the total_sats / reward_entries_complete computation from the Postgres path.
    Returns (computed_reward_btc, reward_entries_complete).
    """
    missing = [bh for bh in matured_hashes if bh not in rewards_sats_by_hash]
    reward_entries_complete = not missing
    total_sats = 0
    for row in selected_rows:
        sats = int(rewards_sats_by_hash.get(str(row["blockhash"]), 0) or 0)
        total_sats += sats
        row["reward_sats"] = sats
    computed = Decimal(total_sats) / Decimal("100000000")
    return computed, reward_entries_complete


def test_reward_complete_when_all_hashes_resolved() -> None:
    rows = [
        {"blockhash": "aaa"},
        {"blockhash": "bbb"},
    ]
    rewards = {"aaa": 100_000_000, "bbb": 50_000_000}
    computed, complete = _compute_settlement_reward(rows, rewards, ["aaa", "bbb"])
    assert complete is True
    assert computed == Decimal("1.5")


def test_reward_incomplete_when_some_hashes_missing() -> None:
    rows = [{"blockhash": "aaa"}, {"blockhash": "bbb"}]
    rewards = {"aaa": 100_000_000}
    computed, complete = _compute_settlement_reward(rows, rewards, ["aaa", "bbb"])
    assert complete is False
    # settlement_reward would be Decimal("0") because not complete
    settlement_reward = computed if complete else Decimal("0")
    assert settlement_reward == Decimal("0")


def test_empty_matured_window_yields_zero_reward() -> None:
    rows: list[dict] = []
    computed, complete = _compute_settlement_reward(rows, {}, [])
    assert complete is True  # no missing hashes → technically complete
    assert computed == Decimal("0")


# ---------------------------------------------------------------------------
# Settlement block linking simulation
# ---------------------------------------------------------------------------

def _simulate_link_blocks(
    blocks: list[dict],
    settlement_id: int,
) -> tuple[int, list[str]]:
    """Simulate bulk_link_settlement_blocks: returns (linked_count, skipped_blockhashes)."""
    linked: dict[str, int] = {}  # blockhash → settlement_id
    inserted = 0
    skipped: list[str] = []
    for block in blocks:
        bh = str(block["blockhash"])
        if bh in linked:
            skipped.append(bh)
        else:
            linked[bh] = settlement_id
            inserted += 1
    return inserted, skipped


def test_bulk_link_links_all_unique_blocks() -> None:
    blocks = [
        {"blockhash": "aaa", "reward_sats": 1000},
        {"blockhash": "bbb", "reward_sats": 2000},
    ]
    inserted, skipped = _simulate_link_blocks(blocks, settlement_id=42)
    assert inserted == 2
    assert skipped == []


def test_bulk_link_skips_duplicate_blockhashes() -> None:
    blocks = [
        {"blockhash": "aaa", "reward_sats": 1000},
        {"blockhash": "aaa", "reward_sats": 1000},
    ]
    inserted, skipped = _simulate_link_blocks(blocks, settlement_id=42)
    assert inserted == 1
    assert skipped == ["aaa"]


def test_bulk_link_empty_blocks_inserts_nothing() -> None:
    inserted, skipped = _simulate_link_blocks([], settlement_id=42)
    assert inserted == 0
    assert skipped == []
