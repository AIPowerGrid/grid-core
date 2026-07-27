# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reconcile production-only constraint drift.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-27

Early production rollouts created the ledger job-id guard as standalone
indexes, and added validator assignment columns to a pre-existing attestation
table without their foreign key. Clean migration-built databases already have
both canonical constraints, so this revision is a no-op there.
"""

import sqlalchemy as sa

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def _has_job_unique(bind) -> bool:
    for constraint in sa.inspect(bind).get_unique_constraints("grid_ledger"):
        if set(constraint.get("column_names") or ()) == {"job_id"}:
            return True
    return False


def _has_assignment_fk(bind) -> bool:
    for constraint in sa.inspect(bind).get_foreign_keys("grid_validator_attestations"):
        if (
            constraint.get("constrained_columns") == ["assignment_id"]
            and constraint.get("referred_table") == "grid_validator_assignments"
            and constraint.get("referred_columns") == ["id"]
        ):
            return True
    return False


def _assert_no_duplicate_jobs(bind) -> None:
    duplicate = bind.execute(
        sa.text(
            "SELECT job_id FROM grid_ledger "
            "GROUP BY job_id HAVING count(*) > 1 LIMIT 1",
        ),
    ).scalar()
    if duplicate is not None:
        raise RuntimeError(
            "grid_ledger contains duplicate job_id rows; refusing to replace "
            "the payout idempotency guard",
        )


def _assert_no_orphaned_assignments(bind) -> None:
    orphan = bind.execute(
        sa.text(
            "SELECT a.assignment_id "
            "FROM grid_validator_attestations AS a "
            "LEFT JOIN grid_validator_assignments AS v ON v.id = a.assignment_id "
            "WHERE a.assignment_id IS NOT NULL AND v.id IS NULL LIMIT 1",
        ),
    ).scalar()
    if orphan is not None:
        raise RuntimeError(
            "grid_validator_attestations contains orphaned assignment_id values; "
            "refusing to add the authoritative-evidence foreign key",
        )


def _drop_standalone_job_indexes(bind) -> None:
    indexes = {
        index["name"]: index
        for index in sa.inspect(bind).get_indexes("grid_ledger")
        if index.get("name")
    }
    for name in ("ix_grid_ledger_job_id", "uq_grid_ledger_job_id"):
        index = indexes.get(name)
        if index and not index.get("duplicates_constraint"):
            op.drop_index(name, table_name="grid_ledger")


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if not _has_job_unique(bind):
        _assert_no_duplicate_jobs(bind)
        indexes = {
            index["name"]: index
            for index in sa.inspect(bind).get_indexes("grid_ledger")
            if index.get("name")
        }
        reusable = indexes.get("uq_grid_ledger_job_id")
        if dialect == "postgresql" and reusable and reusable.get("unique"):
            if "ix_grid_ledger_job_id" in indexes:
                op.drop_index("ix_grid_ledger_job_id", table_name="grid_ledger")
            op.execute(
                "ALTER TABLE grid_ledger "
                "ADD CONSTRAINT uq_grid_ledger_job_id "
                "UNIQUE USING INDEX uq_grid_ledger_job_id",
            )
        elif dialect == "sqlite":
            _drop_standalone_job_indexes(bind)
            with op.batch_alter_table("grid_ledger") as batch:
                batch.create_unique_constraint("uq_grid_ledger_job_id", ["job_id"])
        else:
            _drop_standalone_job_indexes(bind)
            op.create_unique_constraint(
                "uq_grid_ledger_job_id",
                "grid_ledger",
                ["job_id"],
            )
    else:
        _drop_standalone_job_indexes(bind)

    if not _has_assignment_fk(bind):
        _assert_no_orphaned_assignments(bind)
        if dialect == "sqlite":
            with op.batch_alter_table("grid_validator_attestations") as batch:
                batch.create_foreign_key(
                    "fk_grid_validator_attestations_assignment_id",
                    "grid_validator_assignments",
                    ["assignment_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
        else:
            op.create_foreign_key(
                "fk_grid_validator_attestations_assignment_id",
                "grid_validator_attestations",
                "grid_validator_assignments",
                ["assignment_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    # This revision canonicalizes economic and evidence-integrity constraints
    # that migration-built databases already had at 0015. Removing either on a
    # downgrade would reintroduce unsafe production drift, so the repair is
    # intentionally retained while Alembic only moves the revision marker.
    pass
