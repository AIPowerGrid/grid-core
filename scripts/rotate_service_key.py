#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Rotate one backend service key, revoking every previous active key."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grid_api.database import close_database, init_database
from grid_api.services.accounts import rotate_service_key


async def _run(service_id: str) -> None:
    await init_database()
    try:
        key = await rotate_service_key(service_id)
    finally:
        await close_database()
    print(f"service_id={service_id}")
    print(f"api_key={key}")
    print("Every previous key for this service is now revoked.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    args = parser.parse_args()
    asyncio.run(_run(args.id))


if __name__ == "__main__":
    main()
