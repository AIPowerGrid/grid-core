# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Additive SQLite migration compatibility; money execution is PostgreSQL-only."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from grid_api.v2.schema import metadata

NAMES = {
    "grid_validator_compensation_campaigns",
    "grid_validator_compensation_allocations",
    "grid_validator_compensation_work",
}


@pytest.fixture
def migrated():
    path = Path(__file__).resolve().parents[3] / "alembic/versions/0036_validator_compensation_allocations.py"
    spec = importlib.util.spec_from_file_location("compensation_migration", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        metadata.create_all(connection, tables=[table for table in metadata.sorted_tables if table.name not in NAMES])
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
            yield connection, revision
    engine.dispose()


def test_additive_tables_match_metadata_and_empty_downgrade_is_safe(migrated):
    connection, revision = migrated
    inspector = sa.inspect(connection)
    for name in NAMES:
        assert connection.scalar(sa.text(f"SELECT count(*) FROM {name}")) == 0
        actual = {column["name"]: column["nullable"] for column in inspector.get_columns(name)}
        expected = {column.name: column.nullable for column in metadata.tables[name].columns}
        assert actual == expected
    before = set(inspector.get_table_names())
    revision.downgrade()
    assert set(sa.inspect(connection).get_table_names()) == before - NAMES
    revision.upgrade()
    assert set(sa.inspect(connection).get_table_names()) == before


def test_open_campaign_prevents_downgrade_without_erasing_history(migrated):
    connection, revision = migrated
    connection.exec_driver_sql(
        "INSERT INTO grid_validator_compensation_campaigns "
        "(id, contract, contract_hash, budget_atomic, created) "
        "VALUES ('fixture', '{}', 'fixture-digest', 1, '2030-01-01')",
    )
    with pytest.raises(RuntimeError, match="refuse to discard"):
        revision.downgrade()
    assert NAMES <= set(sa.inspect(connection).get_table_names())
    assert connection.scalar(sa.text("SELECT count(*) FROM grid_validator_compensation_campaigns")) == 1
