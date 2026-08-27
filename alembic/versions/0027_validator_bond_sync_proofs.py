# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Persist finalized WorkerRegistry sync proofs and health.

Revision ID: 0027
Revises: 0026
"""

import sqlalchemy as sa

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("grid_validator_reference_workers") as batch:
        batch.add_column(sa.Column("bond_finalized_block_hash", sa.String(66), nullable=True))
        batch.add_column(sa.Column("bond_facet_address", sa.String(42), nullable=True))
        batch.add_column(sa.Column("bond_facet_runtime_hash", sa.String(66), nullable=True))
        batch.add_column(sa.Column("bond_status_reason", sa.String(64), nullable=True))

    op.create_table(
        "grid_validator_bond_sync_state",
        sa.Column("chain_id", sa.BigInteger(), primary_key=True),
        sa.Column("bond_contract", sa.String(42), primary_key=True),
        sa.Column("verifier_version", sa.String(64), nullable=False),
        sa.Column("facet_address", sa.String(42), nullable=True),
        sa.Column("facet_runtime_hash", sa.String(66), nullable=True),
        sa.Column("finalized_block", sa.BigInteger(), nullable=True),
        sa.Column("finalized_block_hash", sa.String(66), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_reason", sa.String(64), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('unverified', 'healthy', 'faulted')",
            name="ck_grid_validator_bond_sync_state_status",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_grid_validator_bond_sync_state_failures",
        ),
        sa.CheckConstraint(
            "finalized_block IS NULL OR finalized_block >= 0",
            name="ck_grid_validator_bond_sync_state_block",
        ),
    )


def downgrade() -> None:
    op.drop_table("grid_validator_bond_sync_state")
    with op.batch_alter_table("grid_validator_reference_workers") as batch:
        batch.drop_column("bond_status_reason")
        batch.drop_column("bond_facet_runtime_hash")
        batch.drop_column("bond_facet_address")
        batch.drop_column("bond_finalized_block_hash")
