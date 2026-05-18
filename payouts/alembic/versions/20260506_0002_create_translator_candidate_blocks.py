"""create translator candidate blocks table

Revision ID: 20260506_0002
Revises: 20260504_0001
Create Date: 2026-05-06 12:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20260506_0002"
down_revision = "20260504_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "translator_candidate_blocks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("found_time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("found_time_unix", sa.BigInteger(), nullable=False),
        sa.Column("blockhash", sa.Text(), nullable=False),
        sa.Column("worker_identity", sa.Text(), nullable=True),
        sa.Column("channel_id", sa.Integer(), nullable=True),
        sa.Column("job_id", sa.Text(), nullable=True),
        sa.Column("extranonce2", sa.Text(), nullable=True),
        sa.Column("ntime", sa.Text(), nullable=True),
        sa.Column("nonce", sa.Text(), nullable=True),
        sa.Column("version", sa.Text(), nullable=True),
        sa.Column("prev_hash", sa.Text(), nullable=True),
        sa.Column("nbits", sa.Text(), nullable=True),
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'sv1_capture_proxy'"),
        ),
        sa.Column(
            "proof_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'translator_submit_reconstructed_block_hash'"),
        ),
        sa.Column("raw_submit_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("raw_job_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "blockhash ~ '^[0-9a-f]{64}$'",
            name="ck_translator_candidate_blocks_blockhash_lower_hex",
        ),
        sa.UniqueConstraint("blockhash", name="uq_translator_candidate_blocks_blockhash"),
    )
    op.create_index(
        "ix_translator_candidate_blocks_found_time",
        "translator_candidate_blocks",
        ["found_time"],
        unique=False,
    )
    op.create_index(
        "ix_translator_candidate_blocks_worker_identity_found_time",
        "translator_candidate_blocks",
        ["worker_identity", "found_time"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_translator_candidate_blocks_worker_identity_found_time",
        table_name="translator_candidate_blocks",
    )
    op.drop_index(
        "ix_translator_candidate_blocks_found_time",
        table_name="translator_candidate_blocks",
    )
    op.drop_table("translator_candidate_blocks")
