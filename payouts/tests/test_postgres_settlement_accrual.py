"""
Test that deferred work accrual is consumed in payout fractions when a real
block reward arrives.

Scenario (mirrors production):
- Two users: baveet (180M accrued) and Ben (89M accrued) from prior deferred cycles.
- Current window work: baveet=3_634_107, Ben=1_658_162.
- Reward > 0 → completed settlement.
- Expected: payout fractions based on MERGED work (accrual + current), not raw current.
- Expected: work_accrual_bucket cleared after settlement.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.postgres_settlement import run_settlement_postgres

ZERO = Decimal("0")

# ──────────────────────────────────────────────────────────────────────────────
# Minimal fake repository
# ──────────────────────────────────────────────────────────────────────────────

class FakeAccrualRepository:
    """In-memory fake that implements exactly the repo surface used by
    run_settlement_postgres + compute_user_contribution_deltas_postgres."""

    def __init__(
        self,
        *,
        metric_snapshots: list[dict],
        accrual_buckets: dict[str, Decimal],  # username → accumulated_work
    ) -> None:
        self._snapshots = metric_snapshots
        # Users: auto-assigned ids 1, 2, ...
        self._users_by_name: dict[str, dict] = {}
        self._users_by_id: dict[int, dict] = {}
        self._next_user_id = 1

        # Pre-seed users for accrual bucket lookup
        for username in accrual_buckets:
            user = self._make_user(username)

        # Accrual buckets: user_id → accumulated_work
        self._accrual_buckets: dict[int, Decimal] = {
            self._users_by_name[uname]["id"]: amt
            for uname, amt in accrual_buckets.items()
        }

        self._settlement_windows: dict[tuple, dict] = {}
        self._next_settlement_id = 1

        # user_work rows: (settlement_id, user_id) → dict
        self.user_work_rows: dict[tuple[int, int], dict] = {}
        # credit rows: (settlement_id, user_id) → dict
        self.credit_rows: dict[tuple[int, int], dict] = {}
        # carry state
        self._carry: dict[str, dict] = {}

    def _make_user(self, username: str) -> dict:
        existing = self._users_by_name.get(username)
        if existing is not None:
            return existing
        row = {"id": self._next_user_id, "username": username, "status": "active"}
        self._users_by_name[username] = row
        self._users_by_id[self._next_user_id] = row
        self._next_user_id += 1
        return row

    # ── User methods ──────────────────────────────────────────────────────────
    def upsert_user(self, username: str, **kwargs) -> dict:
        return self._make_user(username)

    def get_user_by_id(self, user_id: int) -> dict | None:
        return self._users_by_id.get(user_id)

    def get_user_by_username(self, username: str) -> dict | None:
        return self._users_by_name.get(username)

    # ── Settlement window methods ─────────────────────────────────────────────
    def get_latest_settlement_window(self) -> dict | None:
        if not self._settlement_windows:
            return None
        return max(self._settlement_windows.values(), key=lambda r: r["work_window_end"])

    def get_settlement_window_by_range(self, *, work_window_start, work_window_end) -> dict | None:
        return self._settlement_windows.get((work_window_start, work_window_end))

    def upsert_settlement_window(self, **kwargs) -> dict:
        key = (kwargs["work_window_start"], kwargs["work_window_end"])
        existing = self._settlement_windows.get(key)
        if existing is not None:
            existing.update(kwargs)
            return existing
        row = {"id": self._next_settlement_id, **kwargs}
        self._next_settlement_id += 1
        self._settlement_windows[key] = row
        return row

    def update_settlement_window_by_id(self, *, settlement_id: int, **kwargs) -> dict:
        for row in self._settlement_windows.values():
            if row["id"] == settlement_id:
                row.update(kwargs)
                return row
        raise KeyError(f"settlement {settlement_id} not found")

    # ── Metric snapshots (used by compute_user_contribution_deltas_postgres) ──
    def list_raw_miner_snapshot_counters_up_to(self, *, period_end: datetime) -> list[dict]:
        return [r for r in self._snapshots if r["captured_at"] <= period_end]

    # ── Settlement user-work rows ─────────────────────────────────────────────
    def upsert_settlement_user_work(
        self,
        *,
        settlement_id: int,
        user_id: int,
        share_delta: int,
        work_delta: Decimal,
        payout_fraction: Decimal,
    ) -> dict:
        key = (settlement_id, user_id)
        row = {
            "settlement_id": settlement_id,
            "user_id": user_id,
            "share_delta": share_delta,
            "work_delta": work_delta,
            "payout_fraction": payout_fraction,
        }
        self.user_work_rows[key] = row
        return row

    # ── Settlement credits ────────────────────────────────────────────────────
    def upsert_settlement_user_credit(
        self,
        *,
        settlement_id: int,
        user_id: int,
        amount_sats: int,
        idempotency_key: str,
        status: str,
    ) -> dict:
        key = (settlement_id, user_id)
        row = {
            "id": len(self.credit_rows) + 1,
            "settlement_id": settlement_id,
            "user_id": user_id,
            "amount_sats": amount_sats,
            "status": status,
        }
        self.credit_rows[key] = row
        return row

    def list_settlement_user_credits_with_users(self, settlement_id: int) -> list[dict]:
        return [r for r in self.credit_rows.values() if r["settlement_id"] == settlement_id]

    # ── Work accrual bucket ───────────────────────────────────────────────────
    def list_all_work_accrual_buckets(self) -> list[dict]:
        return [
            {"user_id": uid, "accumulated_work": amt}
            for uid, amt in self._accrual_buckets.items()
        ]

    def get_work_accrual_bucket(self, user_id: int) -> dict | None:
        amt = self._accrual_buckets.get(user_id)
        if amt is None:
            return None
        return {"user_id": user_id, "accumulated_work": amt}

    def upsert_work_accrual_bucket(self, *, user_id: int, accumulated_work: Decimal, **kwargs) -> dict:
        self._accrual_buckets[user_id] = accumulated_work
        return {"user_id": user_id, "accumulated_work": accumulated_work}

    # ── Carry state ───────────────────────────────────────────────────────────
    def get_carry_state(self, *, bucket: str = "default") -> dict | None:
        return self._carry.get(bucket)

    def upsert_carry_state(self, *, bucket: str, carry_btc: Decimal, **kwargs) -> dict:
        row = {"bucket": bucket, "carry_btc": carry_btc}
        self._carry[bucket] = row
        return row


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _snapshot(identity: str, accepted_shares_total: int, accepted_work_total: Decimal, captured_at: datetime) -> dict:
    return {
        "identity": identity,
        "channel_id": None,
        "accepted_shares_total": accepted_shares_total,
        "accepted_work_total": accepted_work_total,
        "captured_at": captured_at,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

def test_accrual_consumed_in_payout_fractions() -> None:
    """
    When users have accumulated work in work_accrual_bucket, a completed
    settlement must merge that accrual into payout fractions — not pay based
    solely on the current window's work.
    """
    window_start = datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC)
    window_end   = datetime(2026, 5, 15, 10, 10, 0, tzinfo=UTC)
    now          = window_end + timedelta(minutes=1)

    # Current-window raw work deltas (matching production values)
    baveet_window_work = Decimal("3634107")
    ben_window_work    = Decimal("1658162")

    # Accrued from prior deferred cycles (bucket values)
    baveet_accrued = Decimal("180000000")
    ben_accrued    = Decimal("89000000")

    repo = FakeAccrualRepository(
        metric_snapshots=[
            # Baseline snapshot just before window (gives counter baseline)
            _snapshot("baveet.rig1", 0,      Decimal("0"),              window_start - timedelta(minutes=1)),
            _snapshot("Ben.rig1",    0,      Decimal("0"),              window_start - timedelta(minutes=1)),
            # In-window snapshot (delta = all work done in window)
            _snapshot("baveet.rig1", 121,    baveet_window_work,        window_start + timedelta(minutes=5)),
            _snapshot("Ben.rig1",    60,     ben_window_work,           window_start + timedelta(minutes=5)),
        ],
        accrual_buckets={
            "baveet": baveet_accrued,
            "Ben":    ben_accrued,
        },
    )

    # Real reward arrives — this should be a completed (non-deferred) settlement
    reward_btc = Decimal("1.87500000")

    result = run_settlement_postgres(
        repository=repo,
        now=now,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=lambda _s, _e: float(reward_btc),
        defer_on_zero_reward=True,
        use_work_accrual=True,
        work_window_start=window_start,
        work_window_end=window_end,
    )

    assert result.status == "completed", f"Expected completed, got {result.status}"

    # ── Verify work_delta in settlement_user_work includes accrual ────────────
    baveet_user_id = repo._users_by_name["baveet"]["id"]
    ben_user_id    = repo._users_by_name["Ben"]["id"]
    settlement_id  = result.settlement_id

    baveet_work_row = repo.user_work_rows[(settlement_id, baveet_user_id)]
    ben_work_row    = repo.user_work_rows[(settlement_id, ben_user_id)]

    baveet_stored_work = Decimal(str(baveet_work_row["work_delta"]))
    ben_stored_work    = Decimal(str(ben_work_row["work_delta"]))

    expected_baveet_total = baveet_window_work + baveet_accrued  # 183_634_107
    expected_ben_total    = ben_window_work    + ben_accrued      # 90_658_162

    assert baveet_stored_work == expected_baveet_total, (
        f"baveet work_delta should be {expected_baveet_total} (window + accrual), "
        f"got {baveet_stored_work} — accrual NOT consumed!"
    )
    assert ben_stored_work == expected_ben_total, (
        f"Ben work_delta should be {expected_ben_total} (window + accrual), "
        f"got {ben_stored_work} — accrual NOT consumed!"
    )

    # ── Verify payout fractions are based on merged work ─────────────────────
    total_merged = expected_baveet_total + expected_ben_total
    expected_baveet_fraction = expected_baveet_total / total_merged
    expected_ben_fraction    = expected_ben_total    / total_merged

    baveet_fraction = Decimal(str(baveet_work_row["payout_fraction"]))
    ben_fraction    = Decimal(str(ben_work_row["payout_fraction"]))

    # Allow small rounding tolerance
    tolerance = Decimal("0.0000001")
    assert abs(baveet_fraction - expected_baveet_fraction) < tolerance, (
        f"baveet payout_fraction={baveet_fraction} but expected ~{expected_baveet_fraction:.8f} "
        f"(based on merged work). Got raw-work fraction instead? "
        f"Raw would be {baveet_window_work / (baveet_window_work + ben_window_work):.8f}"
    )
    assert abs(ben_fraction - expected_ben_fraction) < tolerance, (
        f"Ben payout_fraction={ben_fraction} but expected ~{expected_ben_fraction:.8f}"
    )

    # ── Verify accrual buckets are cleared after completed settlement ─────────
    baveet_bucket_after = repo._accrual_buckets.get(baveet_user_id, ZERO)
    ben_bucket_after    = repo._accrual_buckets.get(ben_user_id, ZERO)

    assert baveet_bucket_after == ZERO, (
        f"baveet accrual bucket should be 0 after completed settlement, got {baveet_bucket_after}"
    )
    assert ben_bucket_after == ZERO, (
        f"Ben accrual bucket should be 0 after completed settlement, got {ben_bucket_after}"
    )


def test_deferred_settlement_accumulates_accrual() -> None:
    """
    When pool_reward=0, work is added to accrual bucket (not paid out).
    Next run with real reward must include that accumulated work.
    """
    window_start = datetime(2026, 5, 15, 10, 0, 0, tzinfo=UTC)
    window_end   = datetime(2026, 5, 15, 10, 10, 0, tzinfo=UTC)
    now          = window_end + timedelta(minutes=1)

    baveet_work = Decimal("3634107")
    ben_work    = Decimal("1658162")

    repo = FakeAccrualRepository(
        metric_snapshots=[
            _snapshot("baveet.rig1", 0,   Decimal("0"),    window_start - timedelta(minutes=1)),
            _snapshot("Ben.rig1",    0,   Decimal("0"),    window_start - timedelta(minutes=1)),
            _snapshot("baveet.rig1", 121, baveet_work,     window_start + timedelta(minutes=5)),
            _snapshot("Ben.rig1",    60,  ben_work,        window_start + timedelta(minutes=5)),
        ],
        accrual_buckets={},  # no prior accrual
    )

    # Stale/orphan block → zero reward → deferred
    result = run_settlement_postgres(
        repository=repo,
        now=now,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=lambda _s, _e: 0.0,
        defer_on_zero_reward=True,
        use_work_accrual=True,
        work_window_start=window_start,
        work_window_end=window_end,
    )

    assert result.status == "deferred"

    # Buckets should now hold this window's work
    baveet_user_id = repo._users_by_name["baveet"]["id"]
    ben_user_id    = repo._users_by_name["Ben"]["id"]

    assert repo._accrual_buckets[baveet_user_id] == baveet_work, "baveet work not accrued"
    assert repo._accrual_buckets[ben_user_id]    == ben_work,    "Ben work not accrued"
