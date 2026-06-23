# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""account payout_wallet — decouple payout address from login identity

Adds a nullable `payout_wallet` to grid_accounts. Distinct from `wallet` (the
canonical SIWE login identity, unique): payout_wallet is NOT unique and NOT a
credential, so an OAuth/username operator can point worker earnings at any Base
address — mining-style, no ownership proof. Settlement (worker_ws) prefers
payout_wallet and falls back to `wallet` for SIWE users who never set one.

Nullable + no backfill: existing accounts keep paying to their identity wallet
(or stay unattributed) until they set a payout address.

NOTE: prod grid runs create_all, not alembic. On prod apply the equivalent ALTER
directly:
    ALTER TABLE grid_accounts ADD COLUMN payout_wallet VARCHAR(42);

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("grid_accounts", sa.Column("payout_wallet", sa.String(42), nullable=True))


def downgrade() -> None:
    op.drop_column("grid_accounts", "payout_wallet")
