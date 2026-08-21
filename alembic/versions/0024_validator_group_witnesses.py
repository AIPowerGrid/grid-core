# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Add single-execution media probe-group witnesses.

Revision ID: 0024
Revises: 0023
"""

import sqlalchemy as sa

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("grid_validator_probe_groups") as batch:
        batch.add_column(sa.Column("probe_job_id", sa.String(96), nullable=True))
        batch.add_column(
            sa.Column(
                "probe_status",
                sa.String(24),
                nullable=False,
                server_default="not_started",
            ),
        )
        batch.add_column(
            sa.Column("probe_attempts", sa.Integer(), nullable=False, server_default="0"),
        )
        batch.add_column(sa.Column("probe_lease_expires", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("probe_witnesses", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("probe_witness_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("probe_completed", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_grid_validator_probe_groups_probe_job_id", ["probe_job_id"])
        batch.create_index("ix_grid_validator_probe_groups_probe_status", ["probe_status"])
        batch.create_index(
            "ix_grid_validator_probe_groups_probe_lease_expires",
            ["probe_lease_expires"],
        )


def downgrade() -> None:
    with op.batch_alter_table("grid_validator_probe_groups") as batch:
        batch.drop_index("ix_grid_validator_probe_groups_probe_lease_expires")
        batch.drop_index("ix_grid_validator_probe_groups_probe_status")
        batch.drop_index("ix_grid_validator_probe_groups_probe_job_id")
        batch.drop_column("probe_completed")
        batch.drop_column("probe_witness_hash")
        batch.drop_column("probe_witnesses")
        batch.drop_column("probe_lease_expires")
        batch.drop_column("probe_attempts")
        batch.drop_column("probe_status")
        batch.drop_column("probe_job_id")
