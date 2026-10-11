# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Record future private source cleanup authority, without backfilling it."""

import sqlalchemy as sa

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade():
    pending = sa.text("source_objects IS NOT NULL AND source_cleanup_at IS NULL")
    with op.batch_alter_table("grid_media_jobs") as batch:
        batch.add_column(sa.Column("source_objects", sa.JSON(none_as_null=True), nullable=True))
        batch.add_column(sa.Column("source_cleanup_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("source_cleanup_attempted_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_check_constraint("ck_grid_media_jobs_source_cleanup", "source_cleanup_at IS NULL OR source_objects IS NOT NULL")
        batch.create_index(
            "ix_grid_media_jobs_source_cleanup",
            ["source_cleanup_attempted_at", "created", "job_id"],
            postgresql_where=pending,
            sqlite_where=pending,
        )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM grid_media_jobs WHERE source_objects IS NOT NULL "
            "OR source_cleanup_at IS NOT NULL OR source_cleanup_attempted_at IS NOT NULL",
        )
    ):
        raise RuntimeError("refuse to discard media source retention history")
    with op.batch_alter_table("grid_media_jobs") as batch:
        batch.drop_index("ix_grid_media_jobs_source_cleanup")
        batch.drop_constraint("ck_grid_media_jobs_source_cleanup", type_="check")
        batch.drop_column("source_cleanup_attempted_at")
        batch.drop_column("source_cleanup_at")
        batch.drop_column("source_objects")
