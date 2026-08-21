#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Rotate one backend service key, revoking every previous active key."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grid_api.database import close_database, init_database
from grid_api.services.accounts import generate_api_key, rotate_service_key


async def _run(service_id: str, output: Path) -> None:
    output = output.expanduser().resolve()
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    key = generate_api_key()
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as secret_file:
            secret_file.write(key + "\n")
            secret_file.flush()
            os.fsync(secret_file.fileno())
        await init_database()
        try:
            await rotate_service_key(service_id, replacement_key=key)
        finally:
            await close_database()
    except Exception:
        output.unlink(missing_ok=True)
        raise
    print(f"service_id={service_id}")
    print(f"api_key_file={output}")
    print("Every previous key for this service is now revoked.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True)
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="new, non-existing file for the replacement key (created mode 0600)",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.id, args.output))


if __name__ == "__main__":
    main()
