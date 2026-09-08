# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded operator wallet-consent requests; no recipient approval or payments."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "grid_validator_compensation_requests",
        sa.Column(
            "allocation_hash",
            sa.String(64),
            sa.ForeignKey("grid_validator_compensation_allocations.allocation_hash", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("id", sa.String(68), nullable=False, unique=True),
        sa.Column("operator_account_id", sa.Uuid, sa.ForeignKey("grid_accounts.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("pairing_id", sa.String(68), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("consent", sa.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"), nullable=True),
        sa.Column("recipient_signature", sa.Text, nullable=True),
        sa.Column("node_signature", sa.String(132), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('awaiting_wallet', 'awaiting_node', 'review_required', 'cancelled')",
            name="ck_validator_comp_request_status",
        ),
        sa.CheckConstraint("expires_at > created", name="ck_validator_comp_request_expiry"),
        sa.CheckConstraint(
            "status IN ('awaiting_wallet', 'cancelled') OR (consent IS NOT NULL AND recipient_signature IS NOT NULL)",
            name="ck_validator_comp_request_wallet",
        ),
        sa.CheckConstraint("(status = 'review_required') = (node_signature IS NOT NULL)", name="ck_validator_comp_request_node"),
        sa.CheckConstraint(
            "recipient_signature IS NULL OR length(recipient_signature) <= 16386",
            name="ck_validator_comp_request_signature",
        ),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM grid_validator_compensation_requests")):
        raise RuntimeError("refuse to discard pending validator payout consent")
    op.drop_table("grid_validator_compensation_requests")
