# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Empty cross-rail receipt ownership; all writers must upgrade together."""

import sqlalchemy as sa

from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "grid_payment_receipt_claims",
        sa.Column("claim_key", sa.String(64), primary_key=True),
        sa.Column("chain_id", sa.Integer, nullable=False),
        sa.Column("asset", sa.String(42), nullable=False),
        sa.Column("transaction_hash", sa.String(66), nullable=False),
        sa.Column("owner_kind", sa.String(16), nullable=False),
        sa.Column("owner_ref", sa.String(128), nullable=False),
        sa.Column("purpose", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("purpose_hash", sa.String(64), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("chain_id", "asset", "transaction_hash", name="uq_grid_receipt_chain_asset_tx"),
        sa.UniqueConstraint("owner_kind", "owner_ref", name="uq_grid_receipt_owner"),
        sa.CheckConstraint("chain_id > 0 AND length(claim_key) = 64 AND length(transaction_hash) = 66 AND length(purpose_hash) = 64",
                           name="ck_grid_receipt_identity"),
        sa.CheckConstraint("asset = lower(asset) AND (asset = 'native' OR (length(asset) = 42 AND substr(asset, 1, 2) = '0x'))",
                           name="ck_grid_receipt_asset"),
        sa.CheckConstraint("owner_kind IN ('deposit', 'text_x402', 'video_x402') AND length(owner_ref) > 0",
                           name="ck_grid_receipt_owner"),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM grid_payment_receipt_claims")):
        raise RuntimeError("refuse to discard shared payment receipt ownership")
    op.drop_table("grid_payment_receipt_claims")
