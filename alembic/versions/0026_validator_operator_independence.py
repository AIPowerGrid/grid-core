# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Add governed validator operator-independence qualification state.

Revision ID: 0026
Revises: 0025
"""

import sqlalchemy as sa

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("grid_validators") as batch:
        batch.add_column(sa.Column("operator_group_id", sa.String(96), nullable=True))
        batch.add_column(
            sa.Column(
                "independence_status",
                sa.String(16),
                nullable=False,
                server_default="unreviewed",
            ),
        )
        batch.add_column(sa.Column("qualification_started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("heartbeat_sample_count", sa.Integer(), nullable=False, server_default="0"),
        )
        batch.add_column(sa.Column("last_heartbeat_sampled_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("independence_reviewed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("independence_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("independence_review_ref", sa.String(128), nullable=True))
        batch.create_check_constraint(
            "ck_grid_validators_independence_status",
            "independence_status IN ('unreviewed', 'candidate', 'verified', 'rejected')",
        )
        batch.create_index("ix_grid_validators_operator_group_id", ["operator_group_id"])
        batch.create_index("ix_grid_validators_independence_status", ["independence_status"])
        batch.create_index("ix_grid_validators_independence_expires_at", ["independence_expires_at"])
    with op.batch_alter_table("grid_validators") as batch:
        batch.alter_column(
            "independence_status",
            existing_type=sa.String(16),
            server_default=None,
        )
        batch.alter_column(
            "heartbeat_sample_count",
            existing_type=sa.Integer(),
            server_default=None,
        )


def downgrade() -> None:
    with op.batch_alter_table("grid_validators") as batch:
        batch.drop_index("ix_grid_validators_independence_expires_at")
        batch.drop_index("ix_grid_validators_independence_status")
        batch.drop_index("ix_grid_validators_operator_group_id")
        batch.drop_constraint("ck_grid_validators_independence_status", type_="check")
        batch.drop_column("independence_review_ref")
        batch.drop_column("independence_expires_at")
        batch.drop_column("independence_reviewed_at")
        batch.drop_column("last_heartbeat_sampled_at")
        batch.drop_column("heartbeat_sample_count")
        batch.drop_column("qualification_started_at")
        batch.drop_column("independence_status")
        batch.drop_column("operator_group_id")
