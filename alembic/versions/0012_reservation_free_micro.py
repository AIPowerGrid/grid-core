# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""grid_reservations.free_micro — free-allowance portion of a hold

The durable half of free-first charging: how much of reserved_micro was drawn
from the daily FREE allowance (the rest was held from paid). Settlement restores
free-to-free and refunds paid-to-paid. Nullable-free add with server default 0 —
existing held rows are all-paid, which is exactly what they were.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-07
"""

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(table):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if "free_micro" not in _columns("grid_reservations"):
        op.add_column(
            "grid_reservations",
            sa.Column("free_micro", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        )


def downgrade() -> None:
    if "free_micro" in _columns("grid_reservations"):
        op.drop_column("grid_reservations", "free_micro")
