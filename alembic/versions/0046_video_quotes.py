# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Empty immutable video quotes; no payment, reservation or dispatch authority."""

import sqlalchemy as sa

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "grid_video_quotes",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("job_id", sa.Uuid, nullable=False),
        sa.Column("account_id", sa.Uuid, sa.ForeignKey("grid_accounts.id"), nullable=False),
        sa.Column("idempotency_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("commitment", sa.String(64), nullable=False),
        sa.Column("authorization_nonce", sa.String(66), nullable=False),
        sa.Column("snapshot", sa.JSON, nullable=False),
        sa.Column("amount_micro", sa.BigInteger, nullable=False),
        sa.Column("max_cost_micro", sa.BigInteger, nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "idempotency_hash", name="uq_grid_video_quotes_owner_key"),
        sa.UniqueConstraint("job_id", name="uq_grid_video_quotes_job"),
        sa.UniqueConstraint("authorization_nonce", name="uq_grid_video_quotes_nonce"),
        sa.CheckConstraint("length(idempotency_hash) = 64 AND length(request_hash) = 64 AND "
                           "length(plan_hash) = 64 AND length(commitment) = 64 AND length(authorization_nonce) = 66",
                           name="ck_grid_video_quotes_hashes"),
        sa.CheckConstraint("amount_micro > 0 AND max_cost_micro >= amount_micro", name="ck_grid_video_quotes_amount"),
        sa.CheckConstraint("expires > created", name="ck_grid_video_quotes_expiry"),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM grid_video_quotes")):
        raise RuntimeError("refuse to discard immutable video quote identity")
    op.drop_table("grid_video_quotes")
