"""create summary snapshot tables

Revision ID: 20260507_0003
Revises: 20260506_0002
Create Date: 2026-05-07 11:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260507_0003"
down_revision = "20260506_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "summary_snapshot",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("settlement_id", sa.BigInteger(), nullable=False),
        sa.Column("payout_period_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("payout_period_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("contribution_window_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("contribution_window_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("snapshot_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("accepted_shares_sum", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("accepted_work_sum", sa.Numeric(38, 16), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "payout_period_end > payout_period_start",
            name="ck_summary_snapshot_payout_period_order",
        ),
        sa.CheckConstraint(
            "contribution_window_end > contribution_window_start",
            name="ck_summary_snapshot_contribution_window_order",
        ),
        sa.CheckConstraint(
            "snapshot_count >= 0",
            name="ck_summary_snapshot_snapshot_count_nonnegative",
        ),
        sa.CheckConstraint(
            "accepted_shares_sum >= 0",
            name="ck_summary_snapshot_accepted_shares_sum_nonnegative",
        ),
        sa.CheckConstraint(
            "accepted_work_sum >= 0",
            name="ck_summary_snapshot_accepted_work_sum_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["settlement_id"],
            ["settlement_windows.id"],
            name="fk_summary_snapshot_settlement_id_settlement_windows",
        ),
        sa.UniqueConstraint("settlement_id", name="uq_summary_snapshot_settlement_id"),
    )
    op.create_index(
        "ix_summary_snapshot_contrib_window",
        "summary_snapshot",
        ["contribution_window_start", "contribution_window_end"],
        unique=False,
    )
    op.create_index(
        "ix_summary_snapshot_created_at",
        "summary_snapshot",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "summary_snapshot_miner",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("summary_snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column("worker_identity", sa.Text(), nullable=False),
        sa.Column("worker_name", sa.Text(), nullable=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("snapshot_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("accepted_shares_sum", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("accepted_work_sum", sa.Numeric(38, 16), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "snapshot_count >= 0",
            name="ck_summary_snapshot_miner_snapshot_count_nonnegative",
        ),
        sa.CheckConstraint(
            "accepted_shares_sum >= 0",
            name="ck_summary_snapshot_miner_accepted_shares_sum_nonnegative",
        ),
        sa.CheckConstraint(
            "accepted_work_sum >= 0",
            name="ck_summary_snapshot_miner_accepted_work_sum_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["summary_snapshot_id"],
            ["summary_snapshot.id"],
            name="fk_summary_snapshot_miner_summary_snapshot_id_summary_snapshot",
        ),
        sa.UniqueConstraint(
            "summary_snapshot_id",
            "worker_identity",
            "channel_id",
            name="uq_summary_snapshot_miner_snapshot_worker_channel",
        ),
    )
    op.create_index(
        "ix_summary_snapshot_miner_summary_snapshot_id",
        "summary_snapshot_miner",
        ["summary_snapshot_id"],
        unique=False,
    )
    op.create_index(
        "ix_summary_snapshot_miner_worker_identity_channel_id",
        "summary_snapshot_miner",
        ["worker_identity", "channel_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_summary_snapshot_miner_worker_identity_channel_id",
        table_name="summary_snapshot_miner",
    )
    op.drop_index(
        "ix_summary_snapshot_miner_summary_snapshot_id",
        table_name="summary_snapshot_miner",
    )
    op.drop_table("summary_snapshot_miner")

    op.drop_index(
        "ix_summary_snapshot_created_at",
        table_name="summary_snapshot",
    )
    op.drop_index(
        "ix_summary_snapshot_contrib_window",
        table_name="summary_snapshot",
    )
    op.drop_table("summary_snapshot")
