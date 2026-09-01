# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Add stable private job correlation to validator shadow observations.

Revision ID: 0034
Revises: 0033
"""

import sqlalchemy as sa

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

TABLE = "grid_validator_shadow_observations"


def upgrade() -> None:
    connection = op.get_bind()
    row_count = int(connection.execute(sa.text(f"SELECT count(*) FROM {TABLE}")).scalar_one())
    if row_count:
        raise RuntimeError(
            "0034 requires an empty shadow observation table; existing rows cannot "
            "be linked to raw job ids without fabricating evidence",
        )
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(
            sa.Column("job_ref", sa.String(64), nullable=False, server_default="0" * 64),
        )
        batch.create_check_constraint(
            "ck_grid_validator_shadow_observations_job_ref",
            "length(job_ref) = 64",
        )
        batch.alter_column("job_ref", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(
            "ck_grid_validator_shadow_observations_job_ref",
            type_="check",
        )
        batch.drop_column("job_ref")
