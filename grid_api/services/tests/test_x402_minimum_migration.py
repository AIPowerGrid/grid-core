# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real SQLite upgrade preserves legacy receipts and refuses pricing loss."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations

from grid_api.v2.schema import x402_payments


@pytest.fixture
def migration(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[3]
    path = tmp_path / "minimum.db"
    monkeypatch.setenv("GRID_DB_URL", f"sqlite+aiosqlite:///{path}")
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    command.upgrade(config, "0045")
    spec = importlib.util.spec_from_file_location("minimum_migration", root / "alembic/versions/0046_x402_minimum_charge.py")
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                yield connection, revision
    finally:
        engine.dispose()


def test_upgrade_preserves_legacy_amount_and_allows_zero_history_roundtrip(migration):
    connection, revision = migration
    connection.execute(sa.text(
        "INSERT INTO grid_x402_payments (job_id, authorization_id, payer, network, asset, pay_to, "
        "authorized_micro, settled_micro, status, attempts, created) "
        "VALUES ('old-job', 'old-authorization', 'payer', 'eip155:8453', 'USDC', 'treasury', "
        "30000, 7, 'manual_review', 1, CURRENT_TIMESTAMP)"
    ))
    revision.upgrade()
    assert {column["name"]: column["nullable"] for column in sa.inspect(connection).get_columns("grid_x402_payments")} == {
        column.name: column.nullable for column in x402_payments.c
    }
    assert connection.execute(sa.text(
        "SELECT settled_micro, minimum_charge_micro, status, attempts FROM grid_x402_payments"
    )).one() == (7, 0, "manual_review", 1)
    revision.downgrade()
    assert "minimum_charge_micro" not in {column["name"] for column in sa.inspect(connection).get_columns("grid_x402_payments")}
    assert connection.scalar(sa.text("SELECT settled_micro FROM grid_x402_payments")) == 7
    revision.upgrade()
    assert connection.scalar(sa.text("SELECT minimum_charge_micro FROM grid_x402_payments")) == 0


def test_nonzero_pricing_history_prevents_downgrade(migration):
    connection, revision = migration
    revision.upgrade()
    connection.execute(sa.text(
        "INSERT INTO grid_x402_payments (job_id, authorization_id, payer, network, asset, pay_to, "
        "authorized_micro, minimum_charge_micro, status, attempts, created) "
        "VALUES ('new-job', 'new-authorization', 'payer', 'eip155:8453', 'USDC', 'treasury', "
        "30000, 1000, 'verified', 0, CURRENT_TIMESTAMP)"
    ))
    with pytest.raises(RuntimeError, match="refuse to discard"):
        revision.downgrade()
    assert connection.scalar(sa.text("SELECT minimum_charge_micro FROM grid_x402_payments")) == 1000


@pytest.mark.parametrize("minimum,attempted", [(-1, None), (30001, None), (1000, 999)])
def test_database_rejects_invalid_minimum_or_under_minimum_attempt(migration, minimum, attempted):
    connection, revision = migration
    revision.upgrade()
    with pytest.raises(sa.exc.IntegrityError):
        with connection.begin_nested():
            connection.execute(sa.text(
                "INSERT INTO grid_x402_payments (job_id, authorization_id, payer, network, asset, pay_to, "
                "authorized_micro, minimum_charge_micro, settled_micro, status, attempts, created) "
                "VALUES ('bad-job', 'bad-authorization', 'payer', 'eip155:8453', 'USDC', 'treasury', "
                "30000, :minimum, :attempted, 'verified', 0, CURRENT_TIMESTAMP)"
            ), {"minimum": minimum, "attempted": attempted})
