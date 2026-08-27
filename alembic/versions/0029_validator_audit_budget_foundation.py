# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Add compensated validator-audit budget foundation.

Revision ID: 0029
Revises: 0028
"""

import sqlalchemy as sa

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grid_validator_audit_budget_counters",
        sa.Column("bucket_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("scope", sa.String(16), primary_key=True),
        sa.Column("scope_key", sa.String(192), primary_key=True),
        sa.Column("cap_units", sa.BigInteger(), nullable=False),
        sa.Column("reserved_units", sa.BigInteger(), nullable=False),
        sa.Column("spent_units", sa.BigInteger(), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope IN ('global', 'worker', 'validator', 'pair')",
            name="ck_grid_validator_audit_budget_scope",
        ),
        sa.CheckConstraint(
            "cap_units > 0 AND reserved_units >= 0 AND spent_units >= 0",
            name="ck_grid_validator_audit_budget_nonnegative",
        ),
        sa.CheckConstraint(
            "reserved_units + spent_units <= cap_units",
            name="ck_grid_validator_audit_budget_within_cap",
        ),
    )
    op.create_table(
        "grid_validator_audit_jobs",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column(
            "validator_id",
            sa.String(96),
            sa.ForeignKey("grid_validators.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "target_worker_id",
            sa.Uuid(),
            sa.ForeignKey("grid_workers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("target_worker_name", sa.String(120), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("modality", sa.String(16), nullable=False),
        sa.Column("policy_id", sa.String(128), nullable=False),
        sa.Column("corpus_id", sa.String(128), nullable=False),
        sa.Column("budget_bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reserved_units", sa.BigInteger(), nullable=False),
        sa.Column("actual_units", sa.BigInteger(), nullable=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("result_hash", sa.String(64), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires", sa.DateTime(timezone=True), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('held', 'queued', 'running', 'settled', 'released', 'manual_review')",
            name="ck_grid_validator_audit_jobs_status",
        ),
        sa.CheckConstraint(
            "reserved_units > 0 AND (actual_units IS NULL OR "
            "(actual_units > 0 AND actual_units <= reserved_units))",
            name="ck_grid_validator_audit_jobs_units",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64 AND (result_hash IS NULL OR length(result_hash) = 64)",
            name="ck_grid_validator_audit_jobs_hashes",
        ),
        sa.CheckConstraint(
            "expires > created",
            name="ck_grid_validator_audit_jobs_expiry",
        ),
        sa.CheckConstraint(
            "(status = 'settled' AND actual_units IS NOT NULL AND result_hash IS NOT NULL "
            "AND terminal_at IS NOT NULL) OR "
            "(status = 'released' AND actual_units IS NULL AND result_hash IS NULL "
            "AND terminal_at IS NOT NULL) OR "
            "(status IN ('held', 'queued', 'running') AND actual_units IS NULL "
            "AND result_hash IS NULL AND terminal_at IS NULL) OR "
            "(status = 'manual_review' AND terminal_at IS NOT NULL)",
            name="ck_grid_validator_audit_jobs_terminal",
        ),
        sa.UniqueConstraint("job_id", name="uq_grid_validator_audit_jobs_job_id"),
    )
    op.create_index(
        "ix_grid_validator_audit_jobs_validator_id",
        "grid_validator_audit_jobs",
        ["validator_id"],
    )
    op.create_index(
        "ix_grid_validator_audit_jobs_target_worker_id",
        "grid_validator_audit_jobs",
        ["target_worker_id"],
    )
    op.create_index(
        "ix_grid_validator_audit_jobs_model",
        "grid_validator_audit_jobs",
        ["model"],
    )
    op.create_index(
        "ix_grid_validator_audit_jobs_budget_bucket_start",
        "grid_validator_audit_jobs",
        ["budget_bucket_start"],
    )
    op.create_index(
        "ix_grid_validator_audit_jobs_status",
        "grid_validator_audit_jobs",
        ["status"],
    )
    op.create_index(
        "ix_grid_validator_audit_jobs_created",
        "grid_validator_audit_jobs",
        ["created"],
    )
    op.create_index(
        "ix_grid_validator_audit_jobs_expires",
        "grid_validator_audit_jobs",
        ["expires"],
    )


def downgrade() -> None:
    op.drop_index("ix_grid_validator_audit_jobs_expires", table_name="grid_validator_audit_jobs")
    op.drop_index("ix_grid_validator_audit_jobs_created", table_name="grid_validator_audit_jobs")
    op.drop_index("ix_grid_validator_audit_jobs_status", table_name="grid_validator_audit_jobs")
    op.drop_index(
        "ix_grid_validator_audit_jobs_budget_bucket_start",
        table_name="grid_validator_audit_jobs",
    )
    op.drop_index("ix_grid_validator_audit_jobs_model", table_name="grid_validator_audit_jobs")
    op.drop_index(
        "ix_grid_validator_audit_jobs_target_worker_id",
        table_name="grid_validator_audit_jobs",
    )
    op.drop_index(
        "ix_grid_validator_audit_jobs_validator_id",
        table_name="grid_validator_audit_jobs",
    )
    op.drop_table("grid_validator_audit_jobs")
    op.drop_table("grid_validator_audit_budget_counters")
