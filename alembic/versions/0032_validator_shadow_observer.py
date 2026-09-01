# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Add private, economically inert validator shadow-observer state.

Revision ID: 0032
Revises: 0031
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    portable_json = sa.JSON().with_variant(JSONB(), "postgresql")

    op.create_table(
        "grid_validator_shadow_runs",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("policy_version", sa.String(96), nullable=False),
        sa.Column("policy_config", portable_json, nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("implementation_commit", sa.String(40), nullable=False),
        sa.Column("verification_ref", sa.String(255), nullable=False),
        sa.Column("start_gate", portable_json, nullable=False),
        sa.Column("start_gate_hash", sa.String(64), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_grid_validator_shadow_runs_status",
        ),
        sa.CheckConstraint(
            "length(config_hash) = 64 AND length(start_gate_hash) = 64",
            name="ck_grid_validator_shadow_runs_hashes",
        ),
        sa.CheckConstraint(
            "length(implementation_commit) = 40",
            name="ck_grid_validator_shadow_runs_commit",
        ),
        sa.CheckConstraint(
            "scheduled_end IS NULL OR started IS NOT NULL",
            name="ck_grid_validator_shadow_runs_schedule",
        ),
    )
    op.create_index(
        "ix_grid_validator_shadow_runs_status",
        "grid_validator_shadow_runs",
        ["status"],
    )

    op.create_table(
        "grid_validator_shadow_observations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.String(96),
            sa.ForeignKey("grid_validator_shadow_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("route_ref", sa.String(64), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_version", sa.String(96), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("task_class", sa.String(64), nullable=False),
        sa.Column("modality", sa.String(16), nullable=False),
        sa.Column("requested_capability", sa.String(128), nullable=False),
        sa.Column("candidate_set_hash", sa.String(64), nullable=False),
        sa.Column("candidate_snapshot", portable_json, nullable=False),
        sa.Column("evidence_snapshot", portable_json, nullable=False),
        sa.Column("actual_model", sa.String(255), nullable=False),
        sa.Column("actual_worker_id", sa.String(64), nullable=True),
        sa.Column("hypothetical_model", sa.String(255), nullable=True),
        sa.Column("hypothetical_worker_id", sa.String(64), nullable=True),
        sa.Column("decision_class", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("evidence_window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_commitments", portable_json, nullable=False),
        sa.Column("eligible_operator_count", sa.Integer(), nullable=False),
        sa.Column("mutation_attempted", sa.Boolean(), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "run_id",
            "route_ref",
            name="uq_grid_validator_shadow_observations_run_route",
        ),
        sa.CheckConstraint(
            "decision_class IN ('same', 'would_change', 'would_exclude', 'insufficient_evidence')",
            name="ck_grid_validator_shadow_observations_decision",
        ),
        sa.CheckConstraint(
            "mutation_attempted = false",
            name="ck_grid_validator_shadow_observations_no_mutation",
        ),
        sa.CheckConstraint(
            "eligible_operator_count >= 0",
            name="ck_grid_validator_shadow_observations_operator_count",
        ),
        sa.CheckConstraint(
            "evidence_window_end >= evidence_window_start",
            name="ck_grid_validator_shadow_observations_window",
        ),
        sa.CheckConstraint(
            "length(route_ref) = 64 AND length(payload_hash) = 64 AND length(config_hash) = 64 AND length(candidate_set_hash) = 64",
            name="ck_grid_validator_shadow_observations_hashes",
        ),
    )
    op.create_index(
        "ix_grid_validator_shadow_observations_run_id",
        "grid_validator_shadow_observations",
        ["run_id"],
    )
    op.create_index(
        "ix_grid_validator_shadow_observations_observed_at",
        "grid_validator_shadow_observations",
        ["observed_at"],
    )
    op.create_index(
        "ix_grid_validator_shadow_observations_decision_class",
        "grid_validator_shadow_observations",
        ["decision_class"],
    )
    op.create_index(
        "ix_grid_validator_shadow_observations_reason_code",
        "grid_validator_shadow_observations",
        ["reason_code"],
    )

    op.create_table(
        "grid_validator_shadow_outcomes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "observation_id",
            sa.BigInteger(),
            sa.ForeignKey("grid_validator_shadow_observations.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("actual_worker_id", sa.String(64), nullable=True),
        sa.Column("terminal_status", sa.String(16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "terminal_status IN ('succeeded', 'failed', 'cancelled', 'timeout')",
            name="ck_grid_validator_shadow_outcomes_status",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_grid_validator_shadow_outcomes_duration",
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name="ck_grid_validator_shadow_outcomes_hash",
        ),
    )
    op.create_index(
        "ix_grid_validator_shadow_outcomes_terminal_status",
        "grid_validator_shadow_outcomes",
        ["terminal_status"],
    )
    op.create_index(
        "ix_grid_validator_shadow_outcomes_finished_at",
        "grid_validator_shadow_outcomes",
        ["finished_at"],
    )

    op.create_table(
        "grid_validator_shadow_capacity_samples",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.String(96),
            sa.ForeignKey("grid_validator_shadow_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("verified_independent", sa.Integer(), nullable=False),
        sa.Column("participating_independent", sa.Integer(), nullable=False),
        sa.Column("finalized_independent_groups", sa.Integer(), nullable=False),
        sa.Column("quorum_available", sa.Boolean(), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "run_id",
            "sampled_at",
            name="uq_grid_validator_shadow_capacity_run_sample",
        ),
        sa.CheckConstraint(
            "verified_independent >= 0 AND participating_independent >= 0 AND finalized_independent_groups >= 0",
            name="ck_grid_validator_shadow_capacity_nonnegative",
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name="ck_grid_validator_shadow_capacity_hash",
        ),
    )
    op.create_index(
        "ix_grid_validator_shadow_capacity_samples_run_id",
        "grid_validator_shadow_capacity_samples",
        ["run_id"],
    )
    op.create_index(
        "ix_grid_validator_shadow_capacity_samples_sampled_at",
        "grid_validator_shadow_capacity_samples",
        ["sampled_at"],
    )
    op.create_index(
        "ix_grid_validator_shadow_capacity_samples_quorum_available",
        "grid_validator_shadow_capacity_samples",
        ["quorum_available"],
    )

    op.create_table(
        "grid_validator_shadow_errors",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            sa.String(96),
            sa.ForeignKey("grid_validator_shadow_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "stage IN ('capture', 'evidence', 'policy', 'persist', 'outcome', 'sample')",
            name="ck_grid_validator_shadow_errors_stage",
        ),
    )
    op.create_index(
        "ix_grid_validator_shadow_errors_run_id",
        "grid_validator_shadow_errors",
        ["run_id"],
    )
    op.create_index(
        "ix_grid_validator_shadow_errors_observed_at",
        "grid_validator_shadow_errors",
        ["observed_at"],
    )
    op.create_index(
        "ix_grid_validator_shadow_errors_stage",
        "grid_validator_shadow_errors",
        ["stage"],
    )
    op.create_index(
        "ix_grid_validator_shadow_errors_error_code",
        "grid_validator_shadow_errors",
        ["error_code"],
    )


def downgrade() -> None:
    op.drop_table("grid_validator_shadow_errors")
    op.drop_table("grid_validator_shadow_capacity_samples")
    op.drop_table("grid_validator_shadow_outcomes")
    op.drop_table("grid_validator_shadow_observations")
    op.drop_table("grid_validator_shadow_runs")
