"""create postgres runtime state tables

Revision ID: 20260508_0004
Revises: 20260507_0003
Create Date: 2026-05-08 10:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20260508_0004"
down_revision = "20260507_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "carry_state",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("bucket", sa.Text(), nullable=False),
        sa.Column("carry_btc", sa.Numeric(18, 8), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("carry_btc >= 0", name="ck_carry_state_carry_btc_nonnegative"),
        sa.UniqueConstraint("bucket", name="uq_carry_state_bucket"),
    )

    op.create_table(
        "work_accrual_bucket",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("accumulated_work", sa.Numeric(38, 16), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "accumulated_work >= 0",
            name="ck_work_accrual_bucket_accumulated_work_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_work_accrual_bucket_user_id_users",
        ),
        sa.UniqueConstraint("user_id", name="uq_work_accrual_bucket_user_id"),
    )
    op.create_index(
        "ix_work_accrual_bucket_updated_at",
        "work_accrual_bucket",
        ["updated_at"],
        unique=False,
    )

    op.create_table(
        "payout_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("settlement_credit_id", sa.BigInteger(), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending_sent'")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["settlement_credit_id"],
            ["settlement_user_credits.id"],
            name="fk_payout_events_settlement_credit_id_settlement_user_credits",
        ),
        sa.UniqueConstraint("settlement_credit_id", name="uq_payout_events_settlement_credit_id"),
    )
    op.create_index(
        "ix_payout_events_status",
        "payout_events",
        ["status"],
        unique=False,
    )

    op.create_table(
        "block_counter_state",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("last_blocks_found_total", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "last_blocks_found_total >= 0",
            name="ck_block_counter_state_last_blocks_found_total_nonnegative",
        ),
        sa.UniqueConstraint("channel_id", name="uq_block_counter_state_channel_id"),
    )
    op.create_index(
        "ix_block_counter_state_updated_at",
        "block_counter_state",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_block_counter_state_updated_at", table_name="block_counter_state")
    op.drop_table("block_counter_state")

    op.drop_index("ix_payout_events_status", table_name="payout_events")
    op.drop_table("payout_events")

    op.drop_index("ix_work_accrual_bucket_updated_at", table_name="work_accrual_bucket")
    op.drop_table("work_accrual_bucket")

    op.drop_table("carry_state")
