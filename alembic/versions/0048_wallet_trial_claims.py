# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One wallet trial credit per network address (keyed digest only)."""

import sqlalchemy as sa

from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("grid_wallet_trial_claims"):
        return  # a fresh boot's create_all made it already
    op.create_table(
        "grid_wallet_trial_claims",
        sa.Column("address_hash", sa.String(64), primary_key=True),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("grid_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_grid_wallet_trial_claims_account_id", "grid_wallet_trial_claims", ["account_id"],
    )


def downgrade():
    op.drop_index("ix_grid_wallet_trial_claims_account_id", table_name="grid_wallet_trial_claims")
    op.drop_table("grid_wallet_trial_claims")
