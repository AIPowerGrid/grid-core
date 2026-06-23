# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""prepaid credits — demand-side balance + idempotent ledger

Adds two grid_-namespaced tables:

* grid_credits        — one row per account; prepaid balance in integer
                        micro-USD (USD x 1e6). A cache of the ledger sum.
* grid_credit_ledger  — append-only signed deltas (top-up positive, charge
                        negative) with a UNIQUE `ref` so a retried request or a
                        re-seen deposit can't double-apply.

Ships dark: GRID_CHARGING_ENABLED=0 means the request path only logs the
would-charge amount and never reads or writes these tables, so this is safe to
create ahead of flipping charging on.

NOTE: prod grid runs create_all (checkfirst), so v2 metadata auto-creates these
on the next boot. This migration is the canonical path for alembic-managed DBs.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grid_credits",
        sa.Column("account_id", sa.Uuid, sa.ForeignKey("grid_accounts.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("balance_micro", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "grid_credit_ledger",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Uuid, sa.ForeignKey("grid_accounts.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("delta_micro", sa.BigInteger, nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("ref", sa.String(128), nullable=True, unique=True),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False, index=True),
    )


def downgrade() -> None:
    op.drop_table("grid_credit_ledger")
    op.drop_table("grid_credits")
