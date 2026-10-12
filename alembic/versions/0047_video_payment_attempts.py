# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Empty quote-bound attempts; no spending signatures or funded admission."""

import sqlalchemy as sa

from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "grid_video_payment_attempts",
        sa.Column("quote_id", sa.Uuid, sa.ForeignKey("grid_video_quotes.id"), primary_key=True),
        sa.Column("quote_commitment", sa.String(64), nullable=False),
        sa.Column("authorization_identity", sa.String(64), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("payer", sa.String(42), nullable=False),
        sa.Column("confirmation_tag", sa.String(16), nullable=False),
        sa.Column("min_confirmations", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("transaction_hash", sa.String(66), nullable=True),
        sa.Column("proof", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("proof_hash", sa.String(64), nullable=True),
        sa.Column("proven_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_code", sa.String(32), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("authorization_identity", name="uq_grid_video_attempt_authorization"),
        sa.UniqueConstraint("transaction_hash", name="uq_grid_video_attempt_transaction"),
        sa.CheckConstraint("length(quote_commitment) = 64 AND length(authorization_identity) = 64 AND "
                           "length(payload_hash) = 64 AND length(payer) = 42 AND payer = lower(payer)",
                           name="ck_grid_video_attempt_binding"),
        sa.CheckConstraint("status IN ('settling', 'reported', 'proven', 'manual_review')",
                           name="ck_grid_video_attempt_status"),
        sa.CheckConstraint("confirmation_tag IN ('safe', 'finalized') AND min_confirmations >= 1 AND min_confirmations <= 256",
                           name="ck_grid_video_attempt_policy"),
        sa.CheckConstraint("(status = 'settling' AND transaction_hash IS NULL) OR "
                           "(status IN ('reported', 'proven') AND transaction_hash IS NOT NULL) OR status = 'manual_review'",
                           name="ck_grid_video_attempt_transaction"),
        sa.CheckConstraint("transaction_hash IS NULL OR length(transaction_hash) = 66", name="ck_grid_video_attempt_tx_hash"),
        sa.CheckConstraint("(proof IS NULL AND proof_hash IS NULL AND proven_at IS NULL AND status != 'proven') OR "
                           "(proof IS NOT NULL AND proof_hash IS NOT NULL AND length(proof_hash) = 64 AND "
                           "proven_at IS NOT NULL AND status IN ('proven', 'manual_review'))",
                           name="ck_grid_video_attempt_proof"),
        sa.CheckConstraint("(status = 'manual_review' AND review_code IS NOT NULL AND length(review_code) > 0) OR "
                           "(status != 'manual_review' AND review_code IS NULL)", name="ck_grid_video_attempt_review"),
        sa.Index("ix_grid_video_attempt_reconcile", "status", "created"),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM grid_video_payment_attempts")):
        raise RuntimeError("refuse to discard video payment attempts or unresolved liabilities")
    op.drop_table("grid_video_payment_attempts")
