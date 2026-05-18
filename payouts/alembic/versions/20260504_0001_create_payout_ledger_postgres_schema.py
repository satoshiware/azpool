"""create payout ledger postgres schema

Revision ID: 20260504_0001
Revises:
Create Date: 2026-05-04 10:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20260504_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )

    op.create_table(
        "miner_identities",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("identity", sa.Text(), nullable=False),
        sa.Column("worker_name", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_miner_identities_user_id_users"),
        sa.UniqueConstraint("identity", name="uq_miner_identities_identity"),
    )

    op.create_table(
        "raw_miner_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("captured_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("identity", sa.Text(), nullable=False),
        sa.Column("accepted_shares_total", sa.BigInteger(), nullable=False),
        sa.Column("accepted_work_total", sa.Numeric(38, 16), nullable=False),
        sa.Column(
            "rejected_shares_total",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'translator'")),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint("accepted_shares_total >= 0", name="ck_raw_miner_snapshots_accepted_shares_total_nonnegative"),
        sa.CheckConstraint("rejected_shares_total >= 0", name="ck_raw_miner_snapshots_rejected_shares_total_nonnegative"),
        sa.CheckConstraint("accepted_work_total >= 0", name="ck_raw_miner_snapshots_accepted_work_total_nonnegative"),
    )
    op.create_index(
        "ix_raw_miner_snapshots_captured_at",
        "raw_miner_snapshots",
        ["captured_at"],
        unique=False,
    )
    op.create_index(
        "ix_raw_miner_snapshots_identity_captured_at",
        "raw_miner_snapshots",
        ["identity", "captured_at"],
        unique=False,
    )

    op.create_table(
        "miner_work_deltas",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("identity", sa.Text(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("from_snapshot_id", sa.BigInteger(), nullable=True),
        sa.Column("to_snapshot_id", sa.BigInteger(), nullable=True),
        sa.Column("interval_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("interval_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("share_delta", sa.BigInteger(), nullable=False),
        sa.Column("work_delta", sa.Numeric(38, 16), nullable=False),
        sa.Column(
            "reset_detected",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("interval_end > interval_start", name="ck_miner_work_deltas_interval_order"),
        sa.CheckConstraint("share_delta >= 0", name="ck_miner_work_deltas_share_delta_nonnegative"),
        sa.CheckConstraint("work_delta >= 0", name="ck_miner_work_deltas_work_delta_nonnegative"),
        sa.ForeignKeyConstraint(
            ["from_snapshot_id"],
            ["raw_miner_snapshots.id"],
            name="fk_miner_work_deltas_from_snapshot_id_raw_miner_snapshots",
        ),
        sa.ForeignKeyConstraint(
            ["to_snapshot_id"],
            ["raw_miner_snapshots.id"],
            name="fk_miner_work_deltas_to_snapshot_id_raw_miner_snapshots",
        ),
    )
    op.create_index(
        "ix_miner_work_deltas_interval_start_interval_end",
        "miner_work_deltas",
        ["interval_start", "interval_end"],
        unique=False,
    )
    op.create_index(
        "ix_miner_work_deltas_identity_interval_start_interval_end",
        "miner_work_deltas",
        ["identity", "interval_start", "interval_end"],
        unique=False,
    )

    op.create_table(
        "blocks_found",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("blockhash", sa.Text(), nullable=False),
        sa.Column("found_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("worker_identity", sa.Text(), nullable=True),
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'translator_blocks_found'"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'found'")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("blockhash", name="uq_blocks_found_blockhash"),
    )
    op.create_index("ix_blocks_found_found_at", "blocks_found", ["found_at"], unique=False)

    op.create_table(
        "block_rewards",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("blockhash", sa.Text(), nullable=False),
        sa.Column("reward_sats", sa.BigInteger(), nullable=False),
        sa.Column(
            "reward_source",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'az_block_rewards'"),
        ),
        sa.Column(
            "fetched_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("reward_sats >= 0", name="ck_block_rewards_reward_sats_nonnegative"),
        sa.ForeignKeyConstraint(["blockhash"], ["blocks_found.blockhash"], name="fk_block_rewards_blockhash_blocks_found"),
        sa.UniqueConstraint("blockhash", name="uq_block_rewards_blockhash"),
    )

    op.create_table(
        "settlement_windows",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("settlement_run_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("work_window_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("work_window_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("maturity_offset_minutes", sa.Integer(), nullable=False),
        sa.Column("total_reward_sats", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_work", sa.Numeric(38, 16), nullable=False, server_default=sa.text("0")),
        sa.Column("total_shares", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("work_window_end > work_window_start", name="ck_settlement_windows_work_window_order"),
        sa.CheckConstraint("maturity_offset_minutes > 0", name="ck_settlement_windows_maturity_offset_positive"),
        sa.CheckConstraint("total_reward_sats >= 0", name="ck_settlement_windows_total_reward_nonnegative"),
        sa.CheckConstraint("total_work >= 0", name="ck_settlement_windows_total_work_nonnegative"),
        sa.CheckConstraint("total_shares >= 0", name="ck_settlement_windows_total_shares_nonnegative"),
        sa.UniqueConstraint(
            "work_window_start",
            "work_window_end",
            name="uq_settlement_windows_work_window",
        ),
    )
    op.create_index("ix_settlement_windows_status", "settlement_windows", ["status"], unique=False)

    op.create_table(
        "settlement_blocks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("settlement_id", sa.BigInteger(), nullable=False),
        sa.Column("blockhash", sa.Text(), nullable=False),
        sa.Column("reward_sats", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("reward_sats >= 0", name="ck_settlement_blocks_reward_sats_nonnegative"),
        sa.ForeignKeyConstraint(
            ["settlement_id"],
            ["settlement_windows.id"],
            name="fk_settlement_blocks_settlement_id_settlement_windows",
        ),
        sa.ForeignKeyConstraint(
            ["blockhash"],
            ["blocks_found.blockhash"],
            name="fk_settlement_blocks_blockhash_blocks_found",
        ),
        sa.UniqueConstraint("settlement_id", "blockhash", name="uq_settlement_blocks_settlement_id_blockhash"),
        sa.UniqueConstraint("blockhash", name="uq_settlement_blocks_blockhash"),
    )

    op.create_table(
        "settlement_user_work",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("settlement_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("share_delta", sa.BigInteger(), nullable=False),
        sa.Column("work_delta", sa.Numeric(38, 16), nullable=False),
        sa.Column("payout_fraction", sa.Numeric(38, 18), nullable=False),
        sa.CheckConstraint("share_delta >= 0", name="ck_settlement_user_work_share_delta_nonnegative"),
        sa.CheckConstraint("work_delta >= 0", name="ck_settlement_user_work_work_delta_nonnegative"),
        sa.CheckConstraint("payout_fraction >= 0", name="ck_settlement_user_work_payout_fraction_nonnegative"),
        sa.ForeignKeyConstraint(
            ["settlement_id"],
            ["settlement_windows.id"],
            name="fk_settlement_user_work_settlement_id_settlement_windows",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_settlement_user_work_user_id_users",
        ),
        sa.UniqueConstraint("settlement_id", "user_id", name="uq_settlement_user_work_settlement_id_user_id"),
    )

    op.create_table(
        "settlement_user_credits",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("settlement_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("amount_sats", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("amount_sats >= 0", name="ck_settlement_user_credits_amount_sats_nonnegative"),
        sa.ForeignKeyConstraint(
            ["settlement_id"],
            ["settlement_windows.id"],
            name="fk_settlement_user_credits_settlement_id_settlement_windows",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_settlement_user_credits_user_id_users",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_settlement_user_credits_idempotency_key"),
        sa.UniqueConstraint("settlement_id", "user_id", name="uq_settlement_user_credits_settlement_id_user_id"),
    )
    op.create_index(
        "ix_settlement_user_credits_status",
        "settlement_user_credits",
        ["status"],
        unique=False,
    )

    op.create_table(
        "account_ledger_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("entry_type", sa.Text(), nullable=False),
        sa.Column("amount_sats", sa.BigInteger(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("settlement_credit_id", sa.BigInteger(), nullable=True),
        sa.Column("memo", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("amount_sats > 0", name="ck_account_ledger_entries_amount_sats_positive"),
        sa.CheckConstraint("direction IN ('credit', 'debit')", name="ck_account_ledger_entries_direction"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_account_ledger_entries_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["settlement_credit_id"],
            ["settlement_user_credits.id"],
            name="fk_acct_entries_settlement_credit",
        ),
    )
    op.create_index(
        "ix_account_ledger_entries_user_id_created_at",
        "account_ledger_entries",
        ["user_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "account_balances",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("balance_sats", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("balance_sats >= 0", name="ck_account_balances_balance_sats_nonnegative"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_account_balances_user_id_users"),
        sa.PrimaryKeyConstraint("user_id", name="pk_account_balances"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("payload_hash", sa.Text(), nullable=True),
        sa.Column("previous_hash", sa.Text(), nullable=True),
        sa.Column("event_hash", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"], unique=False)

    op.create_table(
        "service_cursors",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cursor_name", sa.Text(), nullable=False),
        sa.Column("cursor_value", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("cursor_name", name="uq_service_cursors_cursor_name"),
    )


def downgrade() -> None:
    op.drop_table("service_cursors")

    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_table("audit_events")

    op.drop_table("account_balances")

    op.drop_index("ix_account_ledger_entries_user_id_created_at", table_name="account_ledger_entries")
    op.drop_table("account_ledger_entries")

    op.drop_index("ix_settlement_user_credits_status", table_name="settlement_user_credits")
    op.drop_table("settlement_user_credits")

    op.drop_table("settlement_user_work")
    op.drop_table("settlement_blocks")

    op.drop_index("ix_settlement_windows_status", table_name="settlement_windows")
    op.drop_table("settlement_windows")

    op.drop_table("block_rewards")

    op.drop_index("ix_blocks_found_found_at", table_name="blocks_found")
    op.drop_table("blocks_found")

    op.drop_index(
        "ix_miner_work_deltas_identity_interval_start_interval_end",
        table_name="miner_work_deltas",
    )
    op.drop_index(
        "ix_miner_work_deltas_interval_start_interval_end",
        table_name="miner_work_deltas",
    )
    op.drop_table("miner_work_deltas")

    op.drop_index("ix_raw_miner_snapshots_identity_captured_at", table_name="raw_miner_snapshots")
    op.drop_index("ix_raw_miner_snapshots_captured_at", table_name="raw_miner_snapshots")
    op.drop_table("raw_miner_snapshots")

    op.drop_table("miner_identities")
    op.drop_table("users")
