# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""grid_accounts.payout_asset + payout_aipg_bps — worker payout preference

Adds the two nullable columns backing the payout-asset preference (which asset a
worker is paid in + an optional AIPG-slice override). NULL falls back to the grid
defaults, so existing rows need no back-fill.

⚠️ These columns are read on the HOT auth path (accounts.resolve_api_key SELECTs
them on every API key). This migration MUST run before the code that selects them
is deployed, or v2 auth fails globally with "column does not exist" — not just the
payout-preference endpoint. (Prod uses create_all + manual ALTER, not Alembic; this
migration keeps every other deploy path consistent — see docs/architecture notes on
the create_all vs Alembic split.)

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-07
"""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(table):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    cols = _columns("grid_accounts")
    if "payout_asset" not in cols:
        op.add_column("grid_accounts", sa.Column("payout_asset", sa.String(8), nullable=True))
    if "payout_aipg_bps" not in cols:
        op.add_column("grid_accounts", sa.Column("payout_aipg_bps", sa.Integer(), nullable=True))


def downgrade() -> None:
    cols = _columns("grid_accounts")
    if "payout_aipg_bps" in cols:
        op.drop_column("grid_accounts", "payout_aipg_bps")
    if "payout_asset" in cols:
        op.drop_column("grid_accounts", "payout_asset")
