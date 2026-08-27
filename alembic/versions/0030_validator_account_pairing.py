# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Add optional validator/account association, without moving any identity.

Revision ID: 0030
Revises: 0029
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grid_validator_pairings",
        sa.Column("validator_id", sa.String(96), sa.ForeignKey("grid_validators.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("id", sa.String(68), nullable=False, unique=True),
        sa.Column("node_account_id", sa.Uuid(), sa.ForeignKey("grid_accounts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("signing_wallet", sa.String(42), nullable=False),
        sa.Column("operator_account_id", sa.Uuid(), sa.ForeignKey("grid_accounts.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("audience", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("comparison_code", sa.String(12), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'approved', 'linked', 'cancelled')", name="ck_grid_validator_pairings_status"),
        sa.CheckConstraint("expires_at > created", name="ck_grid_validator_pairings_expiry"),
        sa.CheckConstraint(
            "status NOT IN ('approved', 'linked') OR operator_account_id IS NOT NULL", name="ck_grid_validator_pairings_approval",
        ),
        sa.CheckConstraint(
            "operator_account_id IS NULL OR operator_account_id != node_account_id", name="ck_grid_validator_pairings_distinct_accounts",
        ),
    )
    op.create_table(
        "grid_validator_account_links",
        sa.Column("validator_id", sa.String(96), sa.ForeignKey("grid_validators.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("operator_account_id", sa.Uuid(), sa.ForeignKey("grid_accounts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("node_account_id", sa.Uuid(), sa.ForeignKey("grid_accounts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("signing_wallet", sa.String(42), nullable=False),
        sa.Column("pairing_id", sa.String(68), nullable=False, unique=True),
        sa.Column("payload", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("signature", sa.String(132), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("operator_account_id != node_account_id", name="ck_grid_validator_account_links_distinct_accounts"),
        sa.CheckConstraint("revoked_at IS NULL OR revoked_at >= linked_at", name="ck_grid_validator_account_links_revocation"),
    )
    op.create_index("ix_grid_validator_account_links_operator_account_id", "grid_validator_account_links", ["operator_account_id"])


def downgrade() -> None:
    # Explicit rollback discards only association metadata, never node identity.
    op.drop_table("grid_validator_account_links")
    op.drop_table("grid_validator_pairings")
