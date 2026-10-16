# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Snapshot disclosed x402 minimums without repricing historical payments."""

import sqlalchemy as sa

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("grid_x402_payments") as batch:
        batch.add_column(sa.Column("minimum_charge_micro", sa.BigInteger(), nullable=False, server_default=sa.text("0")))
        batch.create_check_constraint(
            "ck_grid_x402_minimum_within_authorization",
            "minimum_charge_micro >= 0 AND minimum_charge_micro <= authorized_micro",
        )
        batch.create_check_constraint("ck_grid_x402_settlement_covers_minimum", "settled_micro IS NULL OR settled_micro >= minimum_charge_micro")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM grid_x402_payments WHERE minimum_charge_micro <> 0")):
        raise RuntimeError("refuse to discard x402 minimum pricing history")
    with op.batch_alter_table("grid_x402_payments") as batch:
        batch.drop_constraint("ck_grid_x402_settlement_covers_minimum", type_="check")
        batch.drop_constraint("ck_grid_x402_minimum_within_authorization", type_="check")
        batch.drop_column("minimum_charge_micro")
