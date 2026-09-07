# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Durable validator transfer bytes and shared nonce accounting; no activation."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade():
    json_type = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table(
        "grid_validator_compensation_payments",
        sa.Column(
            "allocation_hash",
            sa.String(64),
            sa.ForeignKey("grid_validator_compensation_recipients.allocation_hash", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("plan", json_type, nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("chain_id", sa.Integer, nullable=False),
        sa.Column("sender", sa.String(42), nullable=False),
        sa.Column("nonce", sa.BigInteger, nullable=False),
        sa.Column("tx_hash", sa.String(66), nullable=False, unique=True),
        sa.Column("raw_transaction", sa.LargeBinary, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(64), nullable=True),
        sa.Column("receipt", sa.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("chain_id = 8453 AND nonce >= 0", name="ck_validator_comp_payment_chain"),
        sa.CheckConstraint("status IN ('pending', 'sent', 'manual_review')", name="ck_validator_comp_payment_status"),
        sa.CheckConstraint("(status = 'sent') = (receipt IS NOT NULL)", name="ck_validator_comp_payment_receipt"),
        sa.CheckConstraint("length(raw_transaction) > 0 AND length(raw_transaction) <= 2048", name="ck_validator_comp_payment_raw"),
        sa.UniqueConstraint("chain_id", "sender", "nonce", name="uq_validator_comp_payment_nonce"),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM grid_validator_compensation_payments")):
        raise RuntimeError("refuse to discard validator payment history")
    op.drop_table("grid_validator_compensation_payments")
