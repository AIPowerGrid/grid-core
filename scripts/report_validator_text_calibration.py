#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Report aggregate text-probe calibration without exposing probe evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grid_api.config import get_settings
from grid_api.services.validator_text_calibration import (
    DEFAULT_WINDOW_HOURS,
    MAX_WINDOW_HOURS,
    inspect_text_calibration,
)


async def _run(window_hours: int) -> None:
    settings = get_settings()
    engine = create_async_engine(
        settings.async_database_url,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "application_name": "grid_validator_text_calibration",
                "default_transaction_read_only": "on",
            },
        },
    )
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            report = await inspect_text_calibration(
                session,
                window_hours=window_hours,
            )
    finally:
        await engine.dispose()
    print(json.dumps(report, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window-hours",
        type=int,
        default=DEFAULT_WINDOW_HOURS,
        choices=range(1, MAX_WINDOW_HOURS + 1),
        metavar=f"1-{MAX_WINDOW_HOURS}",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.window_hours))


if __name__ == "__main__":
    main()
