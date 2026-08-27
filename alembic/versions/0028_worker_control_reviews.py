# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Add identity-bound worker common-control reviews.

Revision ID: 0028
Revises: 0027
"""

import sqlalchemy as sa

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grid_worker_control_reviews",
        sa.Column(
            "worker_id",
            sa.Uuid(),
            sa.ForeignKey("grid_workers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("grid_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("payout_wallet", sa.String(42), nullable=False),
        sa.Column("operator_group_id", sa.String(96), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_ref", sa.String(128), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('verified', 'rejected', 'revoked')",
            name="ck_grid_worker_control_reviews_status",
        ),
        sa.CheckConstraint(
            "(status = 'verified' AND operator_group_id IS NOT NULL AND expires_at IS NOT NULL) "
            "OR (status IN ('rejected', 'revoked') AND expires_at IS NULL)",
            name="ck_grid_worker_control_reviews_verified_fields",
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at >= reviewed_at",
            name="ck_grid_worker_control_reviews_expiry",
        ),
    )
    op.create_index(
        "ix_grid_worker_control_reviews_account_id",
        "grid_worker_control_reviews",
        ["account_id"],
    )
    op.create_index(
        "ix_grid_worker_control_reviews_payout_wallet",
        "grid_worker_control_reviews",
        ["payout_wallet"],
    )
    op.create_index(
        "ix_grid_worker_control_reviews_operator_group_id",
        "grid_worker_control_reviews",
        ["operator_group_id"],
    )
    op.create_index(
        "ix_grid_worker_control_reviews_status",
        "grid_worker_control_reviews",
        ["status"],
    )
    op.create_index(
        "ix_grid_worker_control_reviews_reviewed_at",
        "grid_worker_control_reviews",
        ["reviewed_at"],
    )
    op.create_index(
        "ix_grid_worker_control_reviews_expires_at",
        "grid_worker_control_reviews",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_grid_worker_control_reviews_expires_at",
        table_name="grid_worker_control_reviews",
    )
    op.drop_index(
        "ix_grid_worker_control_reviews_reviewed_at",
        table_name="grid_worker_control_reviews",
    )
    op.drop_index(
        "ix_grid_worker_control_reviews_status",
        table_name="grid_worker_control_reviews",
    )
    op.drop_index(
        "ix_grid_worker_control_reviews_operator_group_id",
        table_name="grid_worker_control_reviews",
    )
    op.drop_index(
        "ix_grid_worker_control_reviews_payout_wallet",
        table_name="grid_worker_control_reviews",
    )
    op.drop_index(
        "ix_grid_worker_control_reviews_account_id",
        table_name="grid_worker_control_reviews",
    )
    op.drop_table("grid_worker_control_reviews")
