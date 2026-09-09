# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Frozen prospective hourly payouts; no historical adoption or sending."""

import sqlalchemy as sa

from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "grid_payout_periods",
        sa.Column("period_id", sa.String(48), primary_key=True),
        sa.Column("utc_hour", sa.BigInteger, nullable=False, unique=True),
        sa.Column("budget_aipg", sa.Numeric(38, 8), nullable=False),
        sa.Column("plan", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("utc_hour >= 0", name="ck_payout_period_hour"),
        sa.CheckConstraint("budget_aipg >= 0", name="ck_payout_period_budget"),
        sa.CheckConstraint("length(plan_hash) = 64", name="ck_payout_period_hash"),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM grid_payout_periods")):
        raise RuntimeError("refuse to discard frozen worker payout plans")
    op.drop_table("grid_payout_periods")
