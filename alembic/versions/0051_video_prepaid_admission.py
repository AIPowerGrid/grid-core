# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Receipt-backed video admission and explicit, unsent USDC refund obligations."""

import sqlalchemy as sa

from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "grid_video_prepaid_payments",
        sa.Column("quote_id", sa.Uuid, sa.ForeignKey("grid_video_payment_attempts.quote_id"), primary_key=True),
        sa.Column("job_id", sa.String(64), nullable=False, unique=True),
        sa.Column("receipt_claim_key", sa.String(64), sa.ForeignKey("grid_payment_receipt_claims.claim_key"), nullable=False, unique=True),
        sa.Column("quote_commitment", sa.String(64), nullable=False),
        sa.Column("proof_hash", sa.String(64), nullable=False),
        sa.Column("amount_micro", sa.BigInteger, nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=True),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refund_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refund_reason", sa.String(32), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(quote_commitment) = 64 AND length(proof_hash) = 64 AND amount_micro > 0",
                           name="ck_grid_video_prepaid_binding"),
        sa.CheckConstraint("(admitted_at IS NOT NULL AND manifest_hash IS NOT NULL AND length(manifest_hash) = 64) OR "
                           "(admitted_at IS NULL AND manifest_hash IS NULL AND refund_due_at IS NOT NULL)",
                           name="ck_grid_video_prepaid_admission"),
        sa.CheckConstraint("(refund_due_at IS NULL AND refund_reason IS NULL) OR "
                           "(refund_due_at IS NOT NULL AND refund_reason IN "
                           "('quote_expired', 'admission_closed', 'capacity_exceeded', 'execution_failed'))",
                           name="ck_grid_video_prepaid_refund"),
    )
    op.create_index("ix_grid_video_prepaid_refunds", "grid_video_prepaid_payments", ["refund_due_at"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM grid_video_prepaid_payments")):
        raise RuntimeError("retain prepaid video funding and refund obligations")
    op.drop_index("ix_grid_video_prepaid_refunds", table_name="grid_video_prepaid_payments")
    op.drop_table("grid_video_prepaid_payments")
