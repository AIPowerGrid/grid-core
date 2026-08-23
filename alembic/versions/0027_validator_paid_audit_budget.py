# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Add bounded worker compensation for validator audit jobs.

Revision ID: 0027
Revises: 0026
"""

import sqlalchemy as sa

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("grid_validator_assignments") as batch:
        batch.add_column(
            sa.Column(
                "worker_compensation",
                sa.String(24),
                nullable=False,
                server_default="none",
            ),
        )
        batch.create_check_constraint(
            "ck_grid_validator_assignments_worker_compensation",
            "worker_compensation IN ('none', 'audit_budget')",
        )

    op.create_table(
        "grid_validator_audit_budgets",
        sa.Column("budget_day", sa.Date(), primary_key=True),
        sa.Column("limit_den", sa.Numeric(24, 8), nullable=False),
        sa.Column("held_den", sa.Numeric(24, 8), nullable=False, server_default="0"),
        sa.Column("spent_den", sa.Numeric(24, 8), nullable=False, server_default="0"),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("limit_den > 0", name="ck_grid_validator_audit_budget_limit"),
        sa.CheckConstraint("held_den >= 0", name="ck_grid_validator_audit_budget_held"),
        sa.CheckConstraint("spent_den >= 0", name="ck_grid_validator_audit_budget_spent"),
        sa.CheckConstraint(
            "held_den + spent_den <= limit_den",
            name="ck_grid_validator_audit_budget_total",
        ),
    )
    op.create_table(
        "grid_validator_audit_reservations",
        sa.Column("job_id", sa.Uuid(), primary_key=True),
        sa.Column("assignment_id", sa.String(96), nullable=False),
        sa.Column("probe_group_id", sa.String(96), nullable=False),
        sa.Column("worker_id", sa.Uuid(), nullable=False),
        sa.Column("validator_wallet", sa.String(42), nullable=False),
        sa.Column(
            "budget_day",
            sa.Date(),
            sa.ForeignKey("grid_validator_audit_budgets.budget_day", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reserved_den", sa.Numeric(24, 8), nullable=False),
        sa.Column("settled_den", sa.Numeric(24, 8), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="held"),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("reserved_den > 0", name="ck_grid_validator_audit_reserved"),
        sa.CheckConstraint(
            "settled_den IS NULL OR settled_den >= 0",
            name="ck_grid_validator_audit_settled",
        ),
        sa.CheckConstraint(
            "settled_den IS NULL OR settled_den <= reserved_den",
            name="ck_grid_validator_audit_settled_cap",
        ),
        sa.CheckConstraint(
            "status IN ('held', 'settled', 'released')",
            name="ck_grid_validator_audit_status",
        ),
        sa.CheckConstraint(
            "(status = 'held' AND settled_den IS NULL) OR "
            "(status IN ('settled', 'released') AND settled_den IS NOT NULL)",
            name="ck_grid_validator_audit_terminal_amount",
        ),
    )
    for column in (
        "assignment_id",
        "probe_group_id",
        "worker_id",
        "validator_wallet",
        "budget_day",
        "status",
        "created",
    ):
        op.create_index(
            f"ix_grid_validator_audit_reservations_{column}",
            "grid_validator_audit_reservations",
            [column],
        )


def downgrade() -> None:
    for column in reversed((
        "assignment_id",
        "probe_group_id",
        "worker_id",
        "validator_wallet",
        "budget_day",
        "status",
        "created",
    )):
        op.drop_index(
            f"ix_grid_validator_audit_reservations_{column}",
            table_name="grid_validator_audit_reservations",
        )
    op.drop_table("grid_validator_audit_reservations")
    op.drop_table("grid_validator_audit_budgets")
    with op.batch_alter_table("grid_validator_assignments") as batch:
        batch.drop_constraint(
            "ck_grid_validator_assignments_worker_compensation",
            type_="check",
        )
        batch.drop_column("worker_compensation")
