# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""grid_revenue — per-asset distributable revenue pool (pass-through payouts)

New table, so plain create_table works on both SQLite and Postgres. Records what
customers paid per asset per period; workers are paid their den-share of each
pot. See services/settlement/revenue.py.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-07
"""

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("grid_revenue"):
        return
    op.create_table(
        "grid_revenue",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("period_id", sa.String(64), nullable=False),
        sa.Column("asset", sa.String(12), nullable=False),
        sa.Column("amount", sa.Numeric(38, 18), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("ref", sa.String(128), nullable=False, unique=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_grid_revenue_period_id", "grid_revenue", ["period_id"])
    op.create_index("ix_grid_revenue_created", "grid_revenue", ["created"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("grid_revenue"):
        op.drop_index("ix_grid_revenue_created", table_name="grid_revenue")
        op.drop_index("ix_grid_revenue_period_id", table_name="grid_revenue")
        op.drop_table("grid_revenue")
