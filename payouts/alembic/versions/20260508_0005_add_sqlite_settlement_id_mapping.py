"""add sqlite settlement id mapping

Revision ID: 20260508_0005
Revises: 20260508_0004
Create Date: 2026-05-08 10:20:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260508_0005"
down_revision = "20260508_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "settlement_windows",
        sa.Column("sqlite_settlement_id", sa.BigInteger(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_settlement_windows_sqlite_settlement_id",
        "settlement_windows",
        ["sqlite_settlement_id"],
    )
    op.create_index(
        "ix_settlement_windows_sqlite_settlement_id",
        "settlement_windows",
        ["sqlite_settlement_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_settlement_windows_sqlite_settlement_id",
        table_name="settlement_windows",
    )
    op.drop_constraint(
        "uq_settlement_windows_sqlite_settlement_id",
        "settlement_windows",
        type_="unique",
    )
    op.drop_column("settlement_windows", "sqlite_settlement_id")
