# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from grid_api.v2 import schema as tables

NAME = "grid_validator_compensation_recipients"


@pytest.fixture
def migrated():
    path = Path(__file__).resolve().parents[3] / "alembic/versions/0037_validator_compensation_recipients.py"
    spec = importlib.util.spec_from_file_location("recipient_migration", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        tables.metadata.create_all(connection, tables=[t for t in tables.metadata.sorted_tables if t.name != NAME])
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            yield connection, revision
    engine.dispose()


def test_migration_matches_schema_and_empty_roundtrip(migrated):
    connection, revision = migrated
    assert {col["name"]: col["nullable"] for col in sa.inspect(connection).get_columns(NAME)} == {
        col.name: col.nullable for col in tables.validator_compensation_recipients.c
    }
    assert len(sa.inspect(connection).get_foreign_keys(NAME)) == 2
    revision.downgrade()
    assert NAME not in sa.inspect(connection).get_table_names()
    revision.upgrade()
    assert connection.scalar(sa.text(f"SELECT count(*) FROM {NAME}")) == 0


def test_used_consent_history_prevents_downgrade(migrated):
    connection, revision = migrated
    account, now = uuid.uuid4(), datetime.now(UTC)
    connection.execute(sa.insert(tables.accounts).values(id=account))
    connection.execute(
        sa.insert(tables.validator_compensation_campaigns).values(
            id="fixture",
            contract={},
            contract_hash="a" * 64,
            budget_atomic=1,
            created=now,
        ),
    )
    connection.execute(
        sa.insert(tables.validator_compensation_allocations).values(
            campaign_id="fixture",
            operator_group_id="opg_fixture",
            account_id=account,
            reviewed_units=1,
            amount_atomic=1,
            allocation_hash="b" * 64,
            created=now,
        ),
    )
    connection.execute(
        sa.insert(tables.validator_compensation_recipients).values(
            campaign_id="fixture",
            operator_group_id="opg_fixture",
            allocation_hash="b" * 64,
            recipient="0x" + "12" * 20,
            proof={},
            proof_hash="c" * 64,
            created=now,
        ),
    )
    with pytest.raises(RuntimeError, match="refuse to discard"):
        revision.downgrade()
    assert connection.scalar(sa.text(f"SELECT count(*) FROM {NAME}")) == 1
