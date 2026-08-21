# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Add shared validator probe groups and distinct-validator vote guards.

Revision ID: 0022
Revises: 0021
"""

import sqlalchemy as sa

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("grid_validators") as batch:
        batch.create_unique_constraint("uq_grid_validators_account_id", ["account_id"])

    op.create_table(
        "grid_validator_probe_groups",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("target_worker_id", sa.String(64), nullable=False),
        sa.Column("target_worker_name", sa.String(120), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("modality", sa.String(16), nullable=False),
        sa.Column("capability", sa.String(128), nullable=False),
        sa.Column("canary_kind", sa.String(64), nullable=False),
        sa.Column("scoring_policy_id", sa.String(128), nullable=False),
        sa.Column("challenge", sa.JSON(), nullable=False),
        sa.Column("challenge_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("quorum_status", sa.String(24), nullable=False),
        sa.Column("quorum_outcome", sa.String(24), nullable=True),
        sa.Column("quorum_threshold", sa.Integer(), nullable=False),
        sa.Column("target_validator_count", sa.Integer(), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disputed", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finalized", sa.DateTime(timezone=True), nullable=True),
    )
    for column in (
        "target_worker_id",
        "target_worker_name",
        "model",
        "status",
        "quorum_status",
        "created",
        "expires",
    ):
        op.create_index(
            f"ix_grid_validator_probe_groups_{column}",
            "grid_validator_probe_groups",
            [column],
        )

    with op.batch_alter_table("grid_validator_assignments") as batch:
        batch.add_column(sa.Column("probe_group_id", sa.String(96), nullable=True))
        batch.create_foreign_key(
            "fk_grid_validator_assignments_probe_group_id",
            "grid_validator_probe_groups",
            ["probe_group_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_grid_validator_assignments_probe_group_id", ["probe_group_id"])
        batch.create_unique_constraint(
            "uq_grid_validator_assignments_group_validator",
            ["probe_group_id", "validator_id"],
        )

    with op.batch_alter_table("grid_validator_attestations") as batch:
        batch.add_column(sa.Column("probe_group_id", sa.String(96), nullable=True))
        batch.create_foreign_key(
            "fk_grid_validator_attestations_probe_group_id",
            "grid_validator_probe_groups",
            ["probe_group_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_grid_validator_attestations_probe_group_id", ["probe_group_id"])
        batch.create_unique_constraint(
            "uq_grid_validator_attestations_group_validator",
            ["probe_group_id", "validator_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("grid_validator_attestations") as batch:
        batch.drop_constraint(
            "uq_grid_validator_attestations_group_validator",
            type_="unique",
        )
        batch.drop_index("ix_grid_validator_attestations_probe_group_id")
        batch.drop_constraint(
            "fk_grid_validator_attestations_probe_group_id",
            type_="foreignkey",
        )
        batch.drop_column("probe_group_id")

    with op.batch_alter_table("grid_validator_assignments") as batch:
        batch.drop_constraint(
            "uq_grid_validator_assignments_group_validator",
            type_="unique",
        )
        batch.drop_index("ix_grid_validator_assignments_probe_group_id")
        batch.drop_constraint(
            "fk_grid_validator_assignments_probe_group_id",
            type_="foreignkey",
        )
        batch.drop_column("probe_group_id")

    for column in reversed((
        "target_worker_id",
        "target_worker_name",
        "model",
        "status",
        "quorum_status",
        "created",
        "expires",
    )):
        op.drop_index(
            f"ix_grid_validator_probe_groups_{column}",
            table_name="grid_validator_probe_groups",
        )
    op.drop_table("grid_validator_probe_groups")
    with op.batch_alter_table("grid_validators") as batch:
        batch.drop_constraint("uq_grid_validators_account_id", type_="unique")
