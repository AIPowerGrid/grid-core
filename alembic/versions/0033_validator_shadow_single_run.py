# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Allow at most one running validator shadow experiment.

Revision ID: 0033
Revises: 0032
"""

import sqlalchemy as sa

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_grid_validator_shadow_runs_single_running",
        "grid_validator_shadow_runs",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
        sqlite_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_grid_validator_shadow_runs_single_running",
        table_name="grid_validator_shadow_runs",
    )
