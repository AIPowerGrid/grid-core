# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Nullable unpaid-source cleanup metadata; no historical deletion authority."""

import sqlalchemy as sa

from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

_OLD_STATE = "(status = 'staging' AND ready_at IS NULL AND proof IS NULL AND proof_hash IS NULL) OR " \
             "(status = 'ready' AND ready_at IS NOT NULL AND proof IS NOT NULL " \
             "AND proof_hash IS NOT NULL AND length(proof_hash) = 64)"
_NEW_STATE = "(status IN ('staging', 'abandoned') AND ready_at IS NULL AND proof IS NULL AND proof_hash IS NULL) OR " \
             "(status IN ('ready', 'abandoned') AND ready_at IS NOT NULL AND proof IS NOT NULL " \
             "AND proof_hash IS NOT NULL AND length(proof_hash) = 64)"


def upgrade():
    with op.batch_alter_table("grid_video_quote_staging") as batch:
        for name in ("abandoned_at", "cleanup_attempted_at", "cleanup_ack_at"):
            batch.add_column(sa.Column(name, sa.DateTime(timezone=True), nullable=True))
        batch.drop_constraint("ck_grid_video_stage_state", type_="check")
        batch.create_check_constraint("ck_grid_video_stage_state", _NEW_STATE)
        batch.create_check_constraint("ck_grid_video_stage_cleanup",
            "((status = 'abandoned' AND abandoned_at IS NOT NULL) OR "
            "(status != 'abandoned' AND abandoned_at IS NULL)) AND "
            "(cleanup_ack_at IS NULL OR (status = 'abandoned' AND cleanup_attempted_at IS NOT NULL "
            "AND cleanup_ack_at >= cleanup_attempted_at))")
    op.create_index("ix_grid_video_stage_cleanup", "grid_video_quote_staging", ["cleanup_attempted_at", "lease_until"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM grid_video_quote_staging WHERE abandoned_at IS NOT NULL "
                                   "OR cleanup_attempted_at IS NOT NULL OR cleanup_ack_at IS NOT NULL")):
        raise RuntimeError("refuse to discard unpaid video source cleanup history")
    op.drop_index("ix_grid_video_stage_cleanup", table_name="grid_video_quote_staging")
    with op.batch_alter_table("grid_video_quote_staging") as batch:
        batch.drop_constraint("ck_grid_video_stage_cleanup", type_="check")
        batch.drop_constraint("ck_grid_video_stage_state", type_="check")
        batch.create_check_constraint("ck_grid_video_stage_state", _OLD_STATE)
        for name in ("abandoned_at", "cleanup_attempted_at", "cleanup_ack_at"):
            batch.drop_column(name)
