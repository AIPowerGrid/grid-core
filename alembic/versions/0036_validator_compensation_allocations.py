# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Empty private validator pilot contracts and allocations; no payment activation."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade():
    json_type = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table(
        "grid_validator_compensation_campaigns",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("contract", json_type, nullable=False),
        sa.Column("contract_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("budget_atomic", sa.Numeric(78, 0), nullable=False),
        sa.Column("allocated_atomic", sa.Numeric(78, 0), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", json_type, nullable=True),
        sa.CheckConstraint(
            "budget_atomic > 0 AND allocated_atomic >= 0 AND allocated_atomic <= budget_atomic",
            name="ck_validator_comp_campaign_budget",
        ),
        sa.CheckConstraint("status IN ('open', 'finalized')", name="ck_validator_comp_campaign_status"),
        sa.CheckConstraint(
            "(status = 'open' AND finalized_at IS NULL AND result IS NULL AND allocated_atomic = 0) OR "
            "(status = 'finalized' AND finalized_at IS NOT NULL AND result IS NOT NULL)",
            name="ck_validator_comp_campaign_terminal",
        ),
    )
    op.create_table(
        "grid_validator_compensation_allocations",
        sa.Column(
            "campaign_id",
            sa.String(64),
            sa.ForeignKey("grid_validator_compensation_campaigns.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("operator_group_id", sa.String(96), primary_key=True),
        sa.Column("account_id", sa.Uuid, sa.ForeignKey("grid_accounts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("reviewed_units", sa.Integer, nullable=False),
        sa.Column("amount_atomic", sa.Numeric(78, 0), nullable=False),
        sa.Column("allocation_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("reviewed_units > 0 AND amount_atomic >= 0", name="ck_validator_comp_allocation_positive"),
    )
    op.create_table(
        "grid_validator_compensation_work",
        sa.Column("attestation_id", sa.BigInteger, sa.ForeignKey("grid_validator_attestations.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column(
            "campaign_id",
            sa.String(64),
            sa.ForeignKey("grid_validator_compensation_campaigns.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("operator_group_id", sa.String(96), nullable=False),
        sa.Column("probe_group_id", sa.String(96), nullable=False),
        sa.Column("assignment_id", sa.String(96), nullable=False, unique=True),
        sa.Column("evidence_commitment", sa.String(64), nullable=False),
        sa.Column("verification", json_type, nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("operator_group_id", "probe_group_id", name="uq_validator_comp_operator_work"),
        sa.ForeignKeyConstraint(
            ["campaign_id", "operator_group_id"],
            ["grid_validator_compensation_allocations.campaign_id", "grid_validator_compensation_allocations.operator_group_id"],
            ondelete="RESTRICT",
        ),
    )


def downgrade():
    # Allocation history is financial evidence. Operational rollback retains
    # these additive tables; downgrade is only safe on an unused installation.
    if any(
        op.get_bind().scalar(sa.text(f"SELECT count(*) FROM {table}"))
        for table in (
            "grid_validator_compensation_work",
            "grid_validator_compensation_allocations",
            "grid_validator_compensation_campaigns",
        )
    ):
        raise RuntimeError("refuse to discard validator compensation history")
    op.drop_table("grid_validator_compensation_work")
    op.drop_table("grid_validator_compensation_allocations")
    op.drop_table("grid_validator_compensation_campaigns")
