# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Add x402 reservation provenance and settlement receipts.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-27
"""

import sqlalchemy as sa

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("grid_reservations") as batch:
        batch.add_column(
            sa.Column(
                "billing_source",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'credits'"),
            ),
        )
        batch.add_column(sa.Column("external_payer", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("actual_micro", sa.BigInteger(), nullable=True))
        batch.create_index("ix_grid_reservations_billing_source", ["billing_source"])
        batch.create_index("ix_grid_reservations_external_payer", ["external_payer"])

    op.create_table(
        "grid_x402_payments",
        sa.Column("job_id", sa.String(length=64), primary_key=True),
        sa.Column("authorization_id", sa.String(length=64), nullable=False),
        sa.Column("payer", sa.String(length=64), nullable=False),
        sa.Column("network", sa.String(length=64), nullable=False),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("pay_to", sa.String(length=64), nullable=False),
        sa.Column("authorized_micro", sa.BigInteger(), nullable=False),
        sa.Column("settled_micro", sa.BigInteger(), nullable=True),
        sa.Column("tx_hash", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=255), nullable=True),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_attempt", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "authorized_micro > 0",
            name="ck_grid_x402_positive_authorization",
        ),
        sa.CheckConstraint(
            "settled_micro IS NULL OR settled_micro > 0",
            name="ck_grid_x402_positive_settlement",
        ),
        sa.CheckConstraint(
            "settled_micro IS NULL OR settled_micro <= authorized_micro",
            name="ck_grid_x402_settlement_within_authorization",
        ),
        sa.UniqueConstraint(
            "authorization_id",
            name="uq_grid_x402_payments_authorization_id",
        ),
        sa.UniqueConstraint("tx_hash", name="uq_grid_x402_payments_tx_hash"),
    )
    op.create_index("ix_grid_x402_payments_payer", "grid_x402_payments", ["payer"])
    op.create_index("ix_grid_x402_payments_status", "grid_x402_payments", ["status"])
    op.create_index("ix_grid_x402_payments_created", "grid_x402_payments", ["created"])


def downgrade() -> None:
    op.drop_index("ix_grid_x402_payments_created", table_name="grid_x402_payments")
    op.drop_index("ix_grid_x402_payments_status", table_name="grid_x402_payments")
    op.drop_index("ix_grid_x402_payments_payer", table_name="grid_x402_payments")
    op.drop_table("grid_x402_payments")

    with op.batch_alter_table("grid_reservations") as batch:
        batch.drop_index("ix_grid_reservations_external_payer")
        batch.drop_index("ix_grid_reservations_billing_source")
        batch.drop_column("actual_micro")
        batch.drop_column("external_payer")
        batch.drop_column("billing_source")
