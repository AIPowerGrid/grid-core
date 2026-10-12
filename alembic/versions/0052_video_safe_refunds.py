# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Empty reviewed Safe refund intents/proofs; no signer or automatic payments."""

import sqlalchemy as sa

from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "grid_video_refunds",
        sa.Column("quote_id", sa.Uuid, sa.ForeignKey("grid_video_prepaid_payments.quote_id"), primary_key=True),
        sa.Column("chain_id", sa.Integer, nullable=False),
        sa.Column("safe_address", sa.String(42), nullable=False),
        sa.Column("safe_nonce", sa.BigInteger, nullable=False),
        sa.Column("safe_transaction_hash", sa.String(66), nullable=False),
        sa.Column("intent", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("intent_hash", sa.String(64), nullable=False),
        sa.Column("confirmation_tag", sa.String(9), nullable=False),
        sa.Column("min_confirmations", sa.SmallInteger, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("transaction_hash", sa.String(66), nullable=True),
        sa.Column("proof", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("proof_hash", sa.String(64), nullable=True),
        sa.Column("receipt_claim_key", sa.String(64), sa.ForeignKey("grid_payment_receipt_claims.claim_key"), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chain_id", "safe_address", "safe_nonce", name="uq_grid_video_refund_safe_nonce"),
        sa.UniqueConstraint("chain_id", "transaction_hash", name="uq_grid_video_refund_chain_tx"),
        sa.UniqueConstraint("safe_transaction_hash", name="uq_grid_video_refund_safe_hash"),
        sa.UniqueConstraint("receipt_claim_key", name="uq_grid_video_refund_claim"),
        sa.CheckConstraint("chain_id > 0 AND safe_nonce >= 0 AND safe_address = lower(safe_address) AND "
                           "length(safe_address) = 42 AND length(safe_transaction_hash) = 66 AND length(intent_hash) = 64",
                           name="ck_grid_video_refund_binding"),
        sa.CheckConstraint("confirmation_tag IN ('safe', 'finalized') AND min_confirmations BETWEEN 1 AND 256",
                           name="ck_grid_video_refund_policy"),
        sa.CheckConstraint("(status = 'prepared' AND transaction_hash IS NULL AND proof IS NULL AND proof_hash IS NULL AND "
                           "receipt_claim_key IS NULL AND refunded_at IS NULL) OR "
                           "(status = 'refunded' AND transaction_hash IS NOT NULL AND length(transaction_hash) = 66 AND "
                           "proof IS NOT NULL AND proof_hash IS NOT NULL AND length(proof_hash) = 64 AND "
                           "receipt_claim_key IS NOT NULL AND length(receipt_claim_key) = 64 AND refunded_at IS NOT NULL)",
                           name="ck_grid_video_refund_state"),
    )


def downgrade():
    bind = op.get_bind()
    if (bind.scalar(sa.text("SELECT count(*) FROM grid_video_refunds"))
            or bind.scalar(sa.text("SELECT count(*) FROM grid_payment_receipt_claims "
                                   "WHERE owner_kind = 'video_x402' AND owner_ref LIKE 'refund:%'"))):
        raise RuntimeError("retain every video refund intent and receipt")
    op.drop_table("grid_video_refunds")
