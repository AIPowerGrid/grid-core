# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Empty private async media journal; no admission, scheduler or backfill."""

import sqlalchemy as sa

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "grid_media_jobs",
        sa.Column("job_id", sa.String(64), sa.ForeignKey("grid_reservations.job_id"), primary_key=True),
        sa.Column("account_id", sa.Uuid, sa.ForeignKey("grid_accounts.id"), nullable=False),
        sa.Column("idempotency_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("route_models", sa.JSON, nullable=False),
        sa.Column("preferred_worker", sa.String(255), nullable=False, server_default=""),
        sa.Column("max_cost_micro", sa.BigInteger, nullable=False),
        sa.Column("dispatch_state", sa.String(16), nullable=False, server_default="ready"),
        sa.Column("dispatch_token", sa.String(36), nullable=True),
        sa.Column("dispatch_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("queue_message_id", sa.String(64), nullable=True),
        sa.Column("execution_worker_id", sa.Uuid, nullable=True),
        sa.Column("execution_started", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "idempotency_hash", name="uq_grid_media_jobs_owner_key"),
        sa.CheckConstraint("length(idempotency_hash) = 64 AND length(request_hash) = 64 AND length(payload_hash) = 64",
                           name="ck_grid_media_jobs_hashes"),
        sa.CheckConstraint("max_cost_micro > 0 AND dispatch_attempts >= 0", name="ck_grid_media_jobs_bounds"),
        sa.CheckConstraint("dispatch_state IN ('ready', 'dispatching', 'queued')", name="ck_grid_media_jobs_dispatch"),
        sa.CheckConstraint("(dispatch_token IS NULL AND dispatch_deadline IS NULL) OR "
                           "(dispatch_token IS NOT NULL AND dispatch_deadline IS NOT NULL)",
                           name="ck_grid_media_jobs_lease"),
        sa.CheckConstraint("(execution_worker_id IS NULL AND execution_started IS NULL) OR "
                           "(execution_worker_id IS NOT NULL AND execution_started IS NOT NULL)",
                           name="ck_grid_media_jobs_execution"),
    )
    op.create_index("ix_grid_media_jobs_dispatch", "grid_media_jobs", ["dispatch_state", "dispatch_deadline", "created"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM grid_media_jobs")):
        raise RuntimeError("refuse to discard media job identity or execution history")
    op.drop_index("ix_grid_media_jobs_dispatch", table_name="grid_media_jobs")
    op.drop_table("grid_media_jobs")
