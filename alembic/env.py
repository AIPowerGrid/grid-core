# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Async Alembic environment for the v2 grid_ schema.

URL resolution: the same env vars the app uses (POSTGRES_USER/PASS/URL via
grid_api.config). Works against Postgres in prod and SQLite for the
gateway-in-a-box mode (GRID_DB_URL override).
"""

import asyncio
import os
from logging.config import fileConfig

import sqlalchemy as sa
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from grid_api.v2.schema import metadata as target_metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def get_url() -> str:
    # Explicit override first (e.g. sqlite+aiosqlite:///gateway.db),
    # otherwise the app's Postgres settings.
    url = os.environ.get("GRID_DB_URL")
    if url:
        return url
    from grid_api.config import get_settings

    return get_settings().async_database_url


def include_object(obj, name, type_, reflected, compare_to):
    # Only manage our namespace; the legacy Haidra tables (and the
    # grid_den_events table grid_api v1 creates ad hoc) are off limits to
    # autogenerate so a stray `alembic revision --autogenerate` can never
    # emit drops for tables we don't own.
    if type_ == "table":
        return name.startswith("grid_") and name != "grid_den_events"
    return True


def compare_type(context, inspected_column, metadata_column, inspected_type, metadata_type):
    """Treat dialect-reflected JSON and the schema's generic JSON as equivalent."""
    if isinstance(inspected_type, sa.JSON) and isinstance(metadata_type, sa.JSON):
        return False
    return None


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=compare_type,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=compare_type,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = get_url()
    connectable = async_engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
