#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read-only preflight for non-economic validator image and video rollout."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from grid_api.config import get_settings
from grid_api.services import recipes
from grid_api.services.validator_media_readiness import inspect_media_readiness

WORKER_ACTIVE_SET = "grid:workers:active"
WORKER_STATUS_PREFIX = "grid:worker:"
WORKER_STATUS_SUFFIX = ":status"


async def _active_workers(settings) -> list[dict]:
    client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        workers: list[dict] = []
        for worker_id in await client.smembers(WORKER_ACTIVE_SET):
            raw = await client.get(f"{WORKER_STATUS_PREFIX}{worker_id}{WORKER_STATUS_SUFFIX}")
            if raw:
                workers.append(json.loads(raw))
        return workers
    finally:
        await client.aclose()


async def _run(args) -> bool:
    settings = get_settings()
    repo_root = Path(__file__).resolve().parents[1]
    recipes.load_local_recipes(os.fspath(repo_root / "recipes"))
    chain_recipes_loaded = None
    if args.sync_recipes:
        chain_recipes_loaded = await recipes.sync_from_recipevault()
        if chain_recipes_loaded <= 0:
            raise RuntimeError("configured RecipeVault returned no governed recipes")

    engine = create_async_engine(
        settings.async_database_url,
        pool_pre_ping=True,
        connect_args={
            "server_settings": {
                "application_name": "grid_validator_media_readiness",
                "default_transaction_read_only": "on",
            },
        },
    )
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            report = await inspect_media_readiness(
                session,
                await _active_workers(settings),
            )
    finally:
        await engine.dispose()

    report["recipe_catalog"] = {
        "source": "local_and_chain_snapshot" if args.sync_recipes else "local_curated_snapshot",
        "chain_recipes_loaded": chain_recipes_loaded,
    }
    selected = report if args.lane == "all" else {args.lane: report[args.lane]}
    print(json.dumps(selected, indent=2, sort_keys=True))
    if not args.require_ready:
        return True
    lanes = ("image", "video") if args.lane == "all" else (args.lane,)
    return all(bool(report[lane]["ready_to_enable"]) for lane in lanes)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", choices=("image", "video", "all"), default="all")
    parser.add_argument(
        "--sync-recipes",
        action="store_true",
        help="Read the configured RecipeVault into this process before evaluating",
    )
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="Exit non-zero unless every selected lane has passed all rollout gates",
    )
    args = parser.parse_args()
    if args.require_ready and not args.sync_recipes:
        parser.error("--require-ready also requires --sync-recipes")
    try:
        passed = asyncio.run(_run(args))
    except (TypeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
