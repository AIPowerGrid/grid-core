# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Add durable validator probe-result replay.

Revision ID: 0025
Revises: 0024
"""

import sqlalchemy as sa

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("grid_validator_assignments") as batch:
        batch.add_column(sa.Column("probe_result", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("grid_validator_assignments") as batch:
        batch.drop_column("probe_result")
