# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""grid_credit_ledger.ref → NOT NULL (money idempotency invariant)

Every value-moving ledger row must carry a dedup `ref` (the charged job_id or
deposit/event id). The code already refuses a null ref in credit()/debit(), but
the invariant belongs in the DB. This SET NOT NULL fails loudly if any null-ref
row exists — which would itself be a bug to investigate, not silently coerce.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-05
"""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def _columns(table: str):
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(table):
        return {}
    return {c["name"]: c for c in insp.get_columns(table)}


def upgrade() -> None:
    cols = _columns("grid_credit_ledger")
    if "ref" not in cols or cols["ref"].get("nullable") is False:
        return  # table absent (fresh create_all already NOT NULL) or already done
    # Guard: refuse to proceed if any value-moving row lacks a dedup key.
    bind = op.get_bind()
    nulls = bind.execute(
        sa.text("SELECT count(*) FROM grid_credit_ledger WHERE ref IS NULL")
    ).scalar()
    if nulls:
        raise RuntimeError(
            f"{nulls} grid_credit_ledger rows have a NULL ref; resolve before enforcing NOT NULL"
        )
    # batch_alter_table so this works on SQLite too (SQLite can't ALTER COLUMN
    # in place — batch mode recreates the table; a plain ALTER on Postgres).
    with op.batch_alter_table("grid_credit_ledger") as batch_op:
        batch_op.alter_column("ref", existing_type=sa.String(128), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("grid_credit_ledger") as batch_op:
        batch_op.alter_column("ref", existing_type=sa.String(128), nullable=True)
