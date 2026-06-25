# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""custodial worker payouts (grid_payouts)

The account-keyed payout ledger: one row per (period_id, account_id) — accrued
(owed, no wallet) → pending (tx broadcast, bound to a nonce) → sent | failed.
`nonce` binds each payout to the treasury nonce it was sent at; a payout is
settled iff that nonce has mined, which (one payout per nonce) makes re-runs
double-pay-proof.

NOTE: prod grid runs create_all (checkfirst), so v2 metadata auto-creates this
table — and the `nonce` column is added there by a manual ALTER. This migration
is the canonical path for alembic-managed DBs.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-25
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grid_payouts",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("period_id", sa.String(48), nullable=False),
        sa.Column("account_id", sa.Uuid, nullable=True),
        sa.Column("address", sa.String(42), nullable=True),
        sa.Column("den", sa.Float, nullable=False, server_default="0"),
        sa.Column("aipg_amount", sa.Numeric(38, 18), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="accrued"),
        sa.Column("tx_hash", sa.String(66), nullable=True),
        sa.Column("nonce", sa.BigInteger, nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("period_id", "account_id", name="uq_grid_payouts_period_acct"),
    )
    op.create_index("ix_grid_payouts_period_id", "grid_payouts", ["period_id"])
    op.create_index("ix_grid_payouts_account_id", "grid_payouts", ["account_id"])
    op.create_index("ix_grid_payouts_status", "grid_payouts", ["status"])


def downgrade() -> None:
    op.drop_index("ix_grid_payouts_status", table_name="grid_payouts")
    op.drop_index("ix_grid_payouts_account_id", table_name="grid_payouts")
    op.drop_index("ix_grid_payouts_period_id", table_name="grid_payouts")
    op.drop_table("grid_payouts")
