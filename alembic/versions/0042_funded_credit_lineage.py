# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Track externally funded value without changing existing spendable balances."""

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("grid_credits") as batch:
        batch.add_column(sa.Column("funded_balance_micro", sa.BigInteger, nullable=False, server_default="0"))
        batch.create_check_constraint("ck_grid_credits_funded_subset",
            "funded_balance_micro >= 0 AND funded_balance_micro <= CASE WHEN balance_micro > 0 THEN balance_micro ELSE 0 END")
    with op.batch_alter_table("grid_credit_ledger") as batch:
        batch.add_column(sa.Column("funded_delta_micro", sa.BigInteger, nullable=False, server_default="0"))


def downgrade():
    if (op.get_bind().scalar(sa.text("SELECT count(*) FROM grid_credit_ledger WHERE funded_delta_micro <> 0"))
            or op.get_bind().scalar(sa.text("SELECT count(*) FROM grid_credits WHERE funded_balance_micro <> 0"))):
        raise RuntimeError("refuse to discard funded credit lineage")
    with op.batch_alter_table("grid_credit_ledger") as batch:
        batch.drop_column("funded_delta_micro")
    with op.batch_alter_table("grid_credits") as batch:
        batch.drop_constraint("ck_grid_credits_funded_subset", type_="check")
        batch.drop_column("funded_balance_micro")
