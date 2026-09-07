# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Empty allocation-bound recipient consent; no payout activation."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "grid_validator_compensation_recipients",
        sa.Column("campaign_id", sa.String(64), primary_key=True),
        sa.Column("operator_group_id", sa.String(96), primary_key=True),
        sa.Column(
            "allocation_hash",
            sa.String(64),
            sa.ForeignKey("grid_validator_compensation_allocations.allocation_hash", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("recipient", sa.String(42), nullable=False),
        sa.Column("proof", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("proof_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id", "operator_group_id"],
            ["grid_validator_compensation_allocations.campaign_id", "grid_validator_compensation_allocations.operator_group_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("length(recipient) = 42 AND length(proof_hash) = 64", name="ck_validator_comp_recipient_shape"),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM grid_validator_compensation_recipients")):
        raise RuntimeError("refuse to discard validator payout consent history")
    op.drop_table("grid_validator_compensation_recipients")
