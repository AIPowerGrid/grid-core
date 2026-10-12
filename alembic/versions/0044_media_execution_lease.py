# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fence original-worker continuation without reconstructing legacy execution."""

import sqlalchemy as sa

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("grid_media_jobs") as batch:
        batch.add_column(sa.Column("execution_model", sa.String(255), nullable=True))
        batch.add_column(sa.Column("execution_token", sa.String(36), nullable=True))
        batch.add_column(sa.Column("execution_deadline", sa.DateTime(timezone=True), nullable=True))
        batch.create_check_constraint(
            "ck_grid_media_jobs_execution_lease",
            "(execution_model IS NULL AND execution_token IS NULL AND execution_deadline IS NULL) OR "
            "(execution_model IS NOT NULL AND length(execution_model) > 0 AND "
            "execution_token IS NOT NULL AND length(execution_token) = 36 AND "
            "execution_deadline IS NOT NULL AND execution_started IS NOT NULL)",
        )
        batch.create_index("ix_grid_media_jobs_resume", ["execution_worker_id", "execution_deadline", "created"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM grid_media_jobs WHERE execution_token IS NOT NULL")):
        raise RuntimeError("refuse to discard media execution lease history")
    with op.batch_alter_table("grid_media_jobs") as batch:
        batch.drop_index("ix_grid_media_jobs_resume")
        batch.drop_constraint("ck_grid_media_jobs_execution_lease", type_="check")
        batch.drop_column("execution_deadline")
        batch.drop_column("execution_token")
        batch.drop_column("execution_model")
