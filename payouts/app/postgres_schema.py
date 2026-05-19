from __future__ import annotations

from sqlalchemy import (
    BIGINT,
    BOOLEAN,
    TEXT,
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP


metadata = MetaData()

users = Table(
    "users",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("username", TEXT, nullable=False),
    Column("status", TEXT, nullable=False, server_default=text("'active'")),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("username", name="uq_users_username"),
)

miner_identities = Table(
    "miner_identities",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("user_id", BIGINT, ForeignKey("users.id", name="fk_miner_identities_user_id_users"), nullable=False),
    Column("identity", TEXT, nullable=False),
    Column("worker_name", TEXT),
    Column("status", TEXT, nullable=False, server_default=text("'active'")),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("identity", name="uq_miner_identities_identity"),
)

raw_miner_snapshots = Table(
    "raw_miner_snapshots",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("captured_at", TIMESTAMP(timezone=True), nullable=False),
    Column("channel_id", BIGINT),
    Column("identity", TEXT, nullable=False),
    Column("accepted_shares_total", BIGINT, nullable=False),
    Column("accepted_work_total", Numeric(38, 16), nullable=False),
    Column("rejected_shares_total", BIGINT, nullable=False, server_default=text("0")),
    Column("source", TEXT, nullable=False, server_default=text("'translator'")),
    Column("raw_payload", JSONB),
    CheckConstraint(
        "accepted_shares_total >= 0",
        name="ck_raw_miner_snapshots_accepted_shares_total_nonnegative",
    ),
    CheckConstraint(
        "rejected_shares_total >= 0",
        name="ck_raw_miner_snapshots_rejected_shares_total_nonnegative",
    ),
    CheckConstraint(
        "accepted_work_total >= 0",
        name="ck_raw_miner_snapshots_accepted_work_total_nonnegative",
    ),
)
Index("ix_raw_miner_snapshots_captured_at", raw_miner_snapshots.c.captured_at)
Index(
    "ix_raw_miner_snapshots_identity_captured_at",
    raw_miner_snapshots.c.identity,
    raw_miner_snapshots.c.captured_at,
)

miner_work_deltas = Table(
    "miner_work_deltas",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("identity", TEXT, nullable=False),
    Column("channel_id", BIGINT),
    Column(
        "from_snapshot_id",
        BIGINT,
        ForeignKey(
            "raw_miner_snapshots.id",
            name="fk_miner_work_deltas_from_snapshot_id_raw_miner_snapshots",
        ),
    ),
    Column(
        "to_snapshot_id",
        BIGINT,
        ForeignKey(
            "raw_miner_snapshots.id",
            name="fk_miner_work_deltas_to_snapshot_id_raw_miner_snapshots",
        ),
    ),
    Column("interval_start", TIMESTAMP(timezone=True), nullable=False),
    Column("interval_end", TIMESTAMP(timezone=True), nullable=False),
    Column("share_delta", BIGINT, nullable=False),
    Column("work_delta", Numeric(38, 16), nullable=False),
    Column("reset_detected", BOOLEAN, nullable=False, server_default=text("false")),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("interval_end > interval_start", name="ck_miner_work_deltas_interval_order"),
    CheckConstraint("share_delta >= 0", name="ck_miner_work_deltas_share_delta_nonnegative"),
    CheckConstraint("work_delta >= 0", name="ck_miner_work_deltas_work_delta_nonnegative"),
)
Index(
    "ix_miner_work_deltas_interval_start_interval_end",
    miner_work_deltas.c.interval_start,
    miner_work_deltas.c.interval_end,
)
Index(
    "ix_miner_work_deltas_identity_interval_start_interval_end",
    miner_work_deltas.c.identity,
    miner_work_deltas.c.interval_start,
    miner_work_deltas.c.interval_end,
)

blocks_found = Table(
    "blocks_found",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("blockhash", TEXT, nullable=False),
    Column("found_at", TIMESTAMP(timezone=True), nullable=False),
    Column("channel_id", BIGINT),
    Column("worker_identity", TEXT),
    Column("source", TEXT, nullable=False, server_default=text("'translator_blocks_found'")),
    Column("status", TEXT, nullable=False, server_default=text("'found'")),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("blockhash", name="uq_blocks_found_blockhash"),
)
Index("ix_blocks_found_found_at", blocks_found.c.found_at)

block_rewards = Table(
    "block_rewards",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "blockhash",
        TEXT,
        ForeignKey("blocks_found.blockhash", name="fk_block_rewards_blockhash_blocks_found"),
        nullable=False,
    ),
    Column("reward_sats", BIGINT, nullable=False),
    Column("reward_source", TEXT, nullable=False, server_default=text("'az_block_rewards'")),
    Column("fetched_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("reward_sats >= 0", name="ck_block_rewards_reward_sats_nonnegative"),
    UniqueConstraint("blockhash", name="uq_block_rewards_blockhash"),
)

settlement_windows = Table(
    "settlement_windows",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("sqlite_settlement_id", BIGINT),
    Column("status", TEXT, nullable=False, server_default=text("'pending'")),
    Column("settlement_run_at", TIMESTAMP(timezone=True), nullable=False),
    Column("work_window_start", TIMESTAMP(timezone=True), nullable=False),
    Column("work_window_end", TIMESTAMP(timezone=True), nullable=False),
    Column("maturity_offset_minutes", Integer, nullable=False),
    Column("total_reward_sats", BIGINT, nullable=False, server_default=text("0")),
    Column("total_work", Numeric(38, 16), nullable=False, server_default=text("0")),
    Column("total_shares", BIGINT, nullable=False, server_default=text("0")),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    Column("completed_at", TIMESTAMP(timezone=True)),
    CheckConstraint(
        "work_window_end > work_window_start",
        name="ck_settlement_windows_work_window_order",
    ),
    CheckConstraint(
        "maturity_offset_minutes > 0",
        name="ck_settlement_windows_maturity_offset_positive",
    ),
    CheckConstraint(
        "total_reward_sats >= 0",
        name="ck_settlement_windows_total_reward_nonnegative",
    ),
    CheckConstraint("total_work >= 0", name="ck_settlement_windows_total_work_nonnegative"),
    CheckConstraint("total_shares >= 0", name="ck_settlement_windows_total_shares_nonnegative"),
    UniqueConstraint("work_window_start", "work_window_end", name="uq_settlement_windows_work_window"),
    UniqueConstraint("sqlite_settlement_id", name="uq_settlement_windows_sqlite_settlement_id"),
)
Index("ix_settlement_windows_status", settlement_windows.c.status)
Index("ix_settlement_windows_sqlite_settlement_id", settlement_windows.c.sqlite_settlement_id)

summary_snapshot = Table(
    "summary_snapshot",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "settlement_id",
        BIGINT,
        ForeignKey(
            "settlement_windows.id",
            name="fk_summary_snapshot_settlement_id_settlement_windows",
        ),
        nullable=False,
    ),
    Column("payout_period_start", TIMESTAMP(timezone=True), nullable=False),
    Column("payout_period_end", TIMESTAMP(timezone=True), nullable=False),
    Column("contribution_window_start", TIMESTAMP(timezone=True), nullable=False),
    Column("contribution_window_end", TIMESTAMP(timezone=True), nullable=False),
    Column("snapshot_count", BIGINT, nullable=False, server_default=text("0")),
    Column("accepted_shares_sum", BIGINT, nullable=False, server_default=text("0")),
    Column("accepted_work_sum", Numeric(38, 16), nullable=False, server_default=text("0")),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint(
        "payout_period_end > payout_period_start",
        name="ck_summary_snapshot_payout_period_order",
    ),
    CheckConstraint(
        "contribution_window_end > contribution_window_start",
        name="ck_summary_snapshot_contribution_window_order",
    ),
    CheckConstraint(
        "snapshot_count >= 0",
        name="ck_summary_snapshot_snapshot_count_nonnegative",
    ),
    CheckConstraint(
        "accepted_shares_sum >= 0",
        name="ck_summary_snapshot_accepted_shares_sum_nonnegative",
    ),
    CheckConstraint(
        "accepted_work_sum >= 0",
        name="ck_summary_snapshot_accepted_work_sum_nonnegative",
    ),
    UniqueConstraint("settlement_id", name="uq_summary_snapshot_settlement_id"),
)
Index(
    "ix_summary_snapshot_contrib_window",
    summary_snapshot.c.contribution_window_start,
    summary_snapshot.c.contribution_window_end,
)
Index("ix_summary_snapshot_created_at", summary_snapshot.c.created_at)

summary_snapshot_miner = Table(
    "summary_snapshot_miner",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "summary_snapshot_id",
        BIGINT,
        ForeignKey(
            "summary_snapshot.id",
            name="fk_summary_snapshot_miner_summary_snapshot_id_summary_snapshot",
        ),
        nullable=False,
    ),
    Column("worker_identity", TEXT, nullable=False),
    Column("worker_name", TEXT),
    Column("channel_id", BIGINT),
    Column("snapshot_count", BIGINT, nullable=False, server_default=text("0")),
    Column("accepted_shares_sum", BIGINT, nullable=False, server_default=text("0")),
    Column("accepted_work_sum", Numeric(38, 16), nullable=False, server_default=text("0")),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint(
        "snapshot_count >= 0",
        name="ck_summary_snapshot_miner_snapshot_count_nonnegative",
    ),
    CheckConstraint(
        "accepted_shares_sum >= 0",
        name="ck_summary_snapshot_miner_accepted_shares_sum_nonnegative",
    ),
    CheckConstraint(
        "accepted_work_sum >= 0",
        name="ck_summary_snapshot_miner_accepted_work_sum_nonnegative",
    ),
    UniqueConstraint(
        "summary_snapshot_id",
        "worker_identity",
        "channel_id",
        name="uq_summary_snapshot_miner_snapshot_worker_channel",
    ),
)
Index(
    "ix_summary_snapshot_miner_summary_snapshot_id",
    summary_snapshot_miner.c.summary_snapshot_id,
)
Index(
    "ix_summary_snapshot_miner_worker_identity_channel_id",
    summary_snapshot_miner.c.worker_identity,
    summary_snapshot_miner.c.channel_id,
)

settlement_blocks = Table(
    "settlement_blocks",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "settlement_id",
        BIGINT,
        ForeignKey(
            "settlement_windows.id",
            name="fk_settlement_blocks_settlement_id_settlement_windows",
        ),
        nullable=False,
    ),
    Column(
        "blockhash",
        TEXT,
        ForeignKey("blocks_found.blockhash", name="fk_settlement_blocks_blockhash_blocks_found"),
        nullable=False,
    ),
    Column("reward_sats", BIGINT, nullable=False),
    CheckConstraint("reward_sats >= 0", name="ck_settlement_blocks_reward_sats_nonnegative"),
    UniqueConstraint("settlement_id", "blockhash", name="uq_settlement_blocks_settlement_id_blockhash"),
    UniqueConstraint("blockhash", name="uq_settlement_blocks_blockhash"),
)

settlement_user_work = Table(
    "settlement_user_work",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "settlement_id",
        BIGINT,
        ForeignKey(
            "settlement_windows.id",
            name="fk_settlement_user_work_settlement_id_settlement_windows",
        ),
        nullable=False,
    ),
    Column("user_id", BIGINT, ForeignKey("users.id", name="fk_settlement_user_work_user_id_users"), nullable=False),
    Column("share_delta", BIGINT, nullable=False),
    Column("work_delta", Numeric(38, 16), nullable=False),
    Column("payout_fraction", Numeric(38, 18), nullable=False),
    CheckConstraint("share_delta >= 0", name="ck_settlement_user_work_share_delta_nonnegative"),
    CheckConstraint("work_delta >= 0", name="ck_settlement_user_work_work_delta_nonnegative"),
    CheckConstraint(
        "payout_fraction >= 0",
        name="ck_settlement_user_work_payout_fraction_nonnegative",
    ),
    UniqueConstraint("settlement_id", "user_id", name="uq_settlement_user_work_settlement_id_user_id"),
)

settlement_user_credits = Table(
    "settlement_user_credits",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "settlement_id",
        BIGINT,
        ForeignKey(
            "settlement_windows.id",
            name="fk_settlement_user_credits_settlement_id_settlement_windows",
        ),
        nullable=False,
    ),
    Column(
        "user_id",
        BIGINT,
        ForeignKey("users.id", name="fk_settlement_user_credits_user_id_users"),
        nullable=False,
    ),
    Column("amount_sats", BIGINT, nullable=False),
    Column("idempotency_key", TEXT, nullable=False),
    Column("status", TEXT, nullable=False, server_default=text("'pending'")),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("amount_sats >= 0", name="ck_settlement_user_credits_amount_sats_nonnegative"),
    UniqueConstraint("idempotency_key", name="uq_settlement_user_credits_idempotency_key"),
    UniqueConstraint(
        "settlement_id",
        "user_id",
        name="uq_settlement_user_credits_settlement_id_user_id",
    ),
)
Index("ix_settlement_user_credits_status", settlement_user_credits.c.status)

account_ledger_entries = Table(
    "account_ledger_entries",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "user_id",
        BIGINT,
        ForeignKey("users.id", name="fk_account_ledger_entries_user_id_users"),
        nullable=False,
    ),
    Column("entry_type", TEXT, nullable=False),
    Column("amount_sats", BIGINT, nullable=False),
    Column("direction", TEXT, nullable=False),
    Column(
        "settlement_credit_id",
        BIGINT,
        ForeignKey(
            "settlement_user_credits.id",
            name="fk_acct_entries_settlement_credit",
        ),
    ),
    Column("memo", TEXT),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("amount_sats > 0", name="ck_account_ledger_entries_amount_sats_positive"),
    CheckConstraint("direction IN ('credit', 'debit')", name="ck_account_ledger_entries_direction"),
)
Index(
    "ix_account_ledger_entries_user_id_created_at",
    account_ledger_entries.c.user_id,
    account_ledger_entries.c.created_at,
)

account_balances = Table(
    "account_balances",
    metadata,
    Column(
        "user_id",
        BIGINT,
        ForeignKey("users.id", name="fk_account_balances_user_id_users"),
        nullable=False,
    ),
    Column("balance_sats", BIGINT, nullable=False, server_default=text("0")),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("balance_sats >= 0", name="ck_account_balances_balance_sats_nonnegative"),
    PrimaryKeyConstraint("user_id", name="pk_account_balances"),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("event_type", TEXT, nullable=False),
    Column("entity_type", TEXT),
    Column("entity_id", TEXT),
    Column("payload", JSONB),
    Column("payload_hash", TEXT),
    Column("previous_hash", TEXT),
    Column("event_hash", TEXT),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
)
Index("ix_audit_events_created_at", audit_events.c.created_at)

service_cursors = Table(
    "service_cursors",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("cursor_name", TEXT, nullable=False),
    Column("cursor_value", TEXT),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("cursor_name", name="uq_service_cursors_cursor_name"),
)

carry_state = Table(
    "carry_state",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("bucket", TEXT, nullable=False),
    Column("carry_btc", Numeric(18, 8), nullable=False, server_default=text("0")),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("carry_btc >= 0", name="ck_carry_state_carry_btc_nonnegative"),
    UniqueConstraint("bucket", name="uq_carry_state_bucket"),
)

work_accrual_bucket = Table(
    "work_accrual_bucket",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "user_id",
        BIGINT,
        ForeignKey("users.id", name="fk_work_accrual_bucket_user_id_users"),
        nullable=False,
    ),
    Column("accumulated_work", Numeric(38, 16), nullable=False, server_default=text("0")),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint(
        "accumulated_work >= 0",
        name="ck_work_accrual_bucket_accumulated_work_nonnegative",
    ),
    UniqueConstraint("user_id", name="uq_work_accrual_bucket_user_id"),
)
Index("ix_work_accrual_bucket_updated_at", work_accrual_bucket.c.updated_at)

payout_events = Table(
    "payout_events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "settlement_credit_id",
        BIGINT,
        ForeignKey(
            "settlement_user_credits.id",
            name="fk_payout_events_settlement_credit_id_settlement_user_credits",
        ),
        nullable=False,
    ),
    Column("payload_json", JSONB, nullable=False),
    Column("status", TEXT, nullable=False, server_default=text("'pending_sent'")),
    Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("settlement_credit_id", name="uq_payout_events_settlement_credit_id"),
)
Index("ix_payout_events_status", payout_events.c.status)

block_counter_state = Table(
    "block_counter_state",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("channel_id", BIGINT, nullable=False),
    Column("last_blocks_found_total", BIGINT, nullable=False, server_default=text("0")),
    Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint(
        "last_blocks_found_total >= 0",
        name="ck_block_counter_state_last_blocks_found_total_nonnegative",
    ),
    UniqueConstraint("channel_id", name="uq_block_counter_state_channel_id"),
)
Index("ix_block_counter_state_updated_at", block_counter_state.c.updated_at)
