# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""grid_payout_legs — per (period, account, asset) multi-asset payout record

New table; plain create_table works on SQLite + Postgres. Generalizes
grid_payouts (AIPG-only) for the pass-through executor. See
docs/architecture/PAYOUT_EXECUTOR.md.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-07
"""

from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("grid_payout_legs"):
        return
    op.create_table(
        "grid_payout_legs",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("period_id", sa.String(64), nullable=False),
        sa.Column("account_id", sa.Uuid, nullable=False),
        sa.Column("address", sa.String(42), nullable=True),
        sa.Column("asset", sa.String(12), nullable=False),
        sa.Column("rail", sa.String(16), nullable=False),
        sa.Column("amount", sa.Numeric(38, 18), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=True),
        sa.Column("nonce", sa.BigInteger, nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("period_id", "account_id", "asset", name="uq_payout_leg"),
    )
    op.create_index("ix_grid_payout_legs_period_id", "grid_payout_legs", ["period_id"])
    op.create_index("ix_grid_payout_legs_account_id", "grid_payout_legs", ["account_id"])
    op.create_index("ix_grid_payout_legs_created", "grid_payout_legs", ["created"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("grid_payout_legs"):
        for ix in ("created", "account_id", "period_id"):
            op.drop_index(f"ix_grid_payout_legs_{ix}", table_name="grid_payout_legs")
        op.drop_table("grid_payout_legs")
