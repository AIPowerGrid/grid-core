# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Add fail-closed media validator reference-worker snapshots.

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grid_validator_reference_workers",
        sa.Column(
            "worker_id",
            sa.Uuid(),
            sa.ForeignKey("grid_workers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("model", sa.String(255), primary_key=True),
        sa.Column("modality", sa.String(16), primary_key=True),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("grid_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("payout_wallet", sa.String(42), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_reason", sa.String(255), nullable=False),
        sa.Column("bond_contract", sa.String(42), nullable=True),
        sa.Column("bond_chain_id", sa.BigInteger(), nullable=True),
        sa.Column("bond_finalized_block", sa.BigInteger(), nullable=True),
        sa.Column("bond_amount_raw", sa.Numeric(78, 0), nullable=True),
        sa.Column("bond_active", sa.Boolean(), nullable=False),
        sa.Column("bond_slashed", sa.Boolean(), nullable=False),
        sa.Column("bond_verifier_version", sa.String(64), nullable=True),
        sa.Column("bond_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quality_window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quality_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quality_pass_rate", sa.Float(), nullable=True),
        sa.Column("quality_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_selected", sa.DateTime(timezone=True), nullable=True),
        sa.Column("selection_count", sa.BigInteger(), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "modality IN ('image', 'video')",
            name="ck_grid_validator_reference_workers_modality",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'revoked')",
            name="ck_grid_validator_reference_workers_status",
        ),
        sa.CheckConstraint(
            "quality_pass_rate IS NULL OR (quality_pass_rate >= 0 AND quality_pass_rate <= 1)",
            name="ck_grid_validator_reference_workers_quality_pass_rate",
        ),
        sa.CheckConstraint(
            "selection_count >= 0",
            name="ck_grid_validator_reference_workers_selection_count",
        ),
        sa.CheckConstraint(
            "bond_chain_id IS NULL OR bond_chain_id > 0",
            name="ck_grid_validator_reference_workers_chain_id",
        ),
        sa.CheckConstraint(
            "bond_finalized_block IS NULL OR bond_finalized_block >= 0",
            name="ck_grid_validator_reference_workers_finalized_block",
        ),
        sa.CheckConstraint(
            "bond_amount_raw IS NULL OR bond_amount_raw >= 0",
            name="ck_grid_validator_reference_workers_bond_amount",
        ),
        sa.CheckConstraint(
            "quality_window_start IS NULL OR quality_window_end IS NULL OR quality_window_end >= quality_window_start",
            name="ck_grid_validator_reference_workers_quality_window",
        ),
    )
    for column in (
        "account_id",
        "payout_wallet",
        "status",
        "bond_verified_at",
        "quality_reviewed_at",
        "last_selected",
    ):
        op.create_index(
            f"ix_grid_validator_reference_workers_{column}",
            "grid_validator_reference_workers",
            [column],
        )


def downgrade() -> None:
    for column in reversed(
        (
            "account_id",
            "payout_wallet",
            "status",
            "bond_verified_at",
            "quality_reviewed_at",
            "last_selected",
        ),
    ):
        op.drop_index(
            f"ix_grid_validator_reference_workers_{column}",
            table_name="grid_validator_reference_workers",
        )
    op.drop_table("grid_validator_reference_workers")
