# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Empty private prepayment source journal, without payment or dispatch."""

import sqlalchemy as sa

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "grid_video_quote_staging",
        sa.Column("quote_id", sa.Uuid, sa.ForeignKey("grid_video_quotes.id"), primary_key=True),
        sa.Column("quote_commitment", sa.String(64), nullable=False),
        sa.Column("manifest", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("lease_token", sa.Uuid, nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("proof", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("proof_hash", sa.String(64), nullable=True),
        sa.CheckConstraint("length(quote_commitment) = 64 AND length(manifest_hash) = 64 AND lease_until > created",
                           name="ck_grid_video_stage_binding"),
        sa.CheckConstraint("(status = 'staging' AND ready_at IS NULL AND proof IS NULL AND proof_hash IS NULL) OR "
                           "(status = 'ready' AND ready_at IS NOT NULL AND proof IS NOT NULL "
                           "AND proof_hash IS NOT NULL AND length(proof_hash) = 64)",
                           name="ck_grid_video_stage_state"),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM grid_video_quote_staging")):
        raise RuntimeError("refuse to discard private video source authority")
    op.drop_table("grid_video_quote_staging")
