# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Add immutable Base funding receipts.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-27
"""

import sqlalchemy as sa

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grid_deposits",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("grid_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("asset", sa.String(length=12), nullable=False),
        sa.Column("token_address", sa.String(length=42), nullable=True),
        sa.Column("tx_hash", sa.String(length=66), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("from_address", sa.String(length=42), nullable=False),
        sa.Column("treasury_address", sa.String(length=42), nullable=False),
        sa.Column("amount_raw", sa.Numeric(precision=78, scale=0), nullable=False),
        sa.Column("amount_decimals", sa.Integer(), nullable=False),
        sa.Column("price_micro", sa.BigInteger(), nullable=False),
        sa.Column("price_source", sa.String(length=128), nullable=False),
        sa.Column("price_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price_block", sa.BigInteger(), nullable=True),
        sa.Column("credited_micro", sa.BigInteger(), nullable=False),
        sa.Column("refund_address", sa.String(length=42), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_raw > 0", name="ck_grid_deposit_positive_amount"),
        sa.CheckConstraint("credited_micro > 0", name="ck_grid_deposit_positive_credit"),
        sa.UniqueConstraint(
            "chain_id",
            "asset",
            "tx_hash",
            name="uq_grid_deposit_chain_asset_tx",
        ),
    )
    op.create_index("ix_grid_deposits_account_id", "grid_deposits", ["account_id"])
    op.create_index("ix_grid_deposits_asset", "grid_deposits", ["asset"])
    op.create_index("ix_grid_deposits_status", "grid_deposits", ["status"])
    op.create_index("ix_grid_deposits_created", "grid_deposits", ["created"])


def downgrade() -> None:
    op.drop_index("ix_grid_deposits_created", table_name="grid_deposits")
    op.drop_index("ix_grid_deposits_status", table_name="grid_deposits")
    op.drop_index("ix_grid_deposits_asset", table_name="grid_deposits")
    op.drop_index("ix_grid_deposits_account_id", table_name="grid_deposits")
    op.drop_table("grid_deposits")
