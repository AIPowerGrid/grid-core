# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real Core crash/reconnect, private PG/Redis, synthetic worker output/storage.

The native worker journal/GPU recovery is qualified separately. This tests real
scoped registration, original-worker SQL binding, WS resume envelopes and money.
"""

import json
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from websockets.asyncio.client import connect

from grid_api.routers.tests.test_core_process_crash import eventually, frame
from grid_api.routers.tests.test_core_process_crash import pytestmark as pytestmark
from grid_api.routers.tests.test_core_process_crash import rig as rig
from grid_api.v2 import schema as tables

MODEL = "LTX-2.5 Distilled BF16"


async def registered(core, credential, name="resume-fixture"):
    ws = await connect(f"ws://127.0.0.1:{core.port}/v1/workers/ws")
    try:
        await ws.send(
            json.dumps(
                {
                    "apikey": credential,
                    "name": name,
                    "models": [MODEL],
                    "job_types": ["video"],
                    "media_features": ["async-video-resume-v1", "recipe-image-bindings-v1"],
                    "bridge_agent": "resume-fixture/ws:1",
                },
            ),
        )
        ready = await frame(ws, "ready")
        return ws, ready["worker_id"]
    except BaseException:
        await ws.close()
        raise


@pytest.mark.parametrize("phase", ["dispatched", "committed"])
async def test_real_core_restart_preserves_one_job_charge_and_original_worker(rig, phase):
    core, engine, redis, account, key, worker_key = rig
    await core.kill()
    core.env.update(TEST_ASYNC_VIDEO="1", MEDIA_ASYNC_ENABLED="1", MEDIA_JOB_SWEEP_SECONDS="5")
    await core.start("committed" if phase == "committed" else "none")
    first, worker_id = await registered(core, worker_key)
    idempotency = str(uuid4())
    async with httpx.AsyncClient(base_url=core.url, headers={"apikey": key}, trust_env=False, timeout=15) as client:
        try:
            body = {
                "model": MODEL,
                "prompt": "Synthetic video test",
                "seed": 31,
                "frames": 33,
                "size": "512x320",
                "max_cost_micro_usd": 100_000,
            }
            created = await client.post("/v1/videos/jobs", headers={"Idempotency-Key": idempotency}, json=body)
            assert created.status_code == 202, created.text
            original = await frame(first, "job")
            assert original["id"] == created.json()["job_id"] and "resume" not in original
            job_id = original["id"]
            if phase == "committed":
                await first.send(json.dumps({"type": "done", "id": job_id, "results": [{"index": 0, "sha256": "a" * 64, "seed": 31}]}))

                async def committed():
                    async with engine.connect() as connection:
                        row = (
                            await connection.execute(
                                sa.select(tables.reservations.c.media_result, tables.reservations.c.status).where(
                                    tables.reservations.c.job_id == job_id,
                                ),
                            )
                        ).one()
                        return row.media_result and row.status == "settled" and (core.path / "barrier").exists()

                await eventually(committed)
            assert await core.kill() < 0
        finally:
            await first.close()
        # Admission disabled and all Redis queue state destroyed: only original
        # SQL execution, not a new job/hold or Redis delivery, can recover it.
        await redis.flushdb()
        core.env["MEDIA_ASYNC_ENABLED"] = "0"
        await core.start()
        second, same_worker_id = await registered(core, worker_key)
        try:
            assert same_worker_id == worker_id
            if phase == "dispatched":
                resumed = await frame(second, "job")
                assert resumed["resume"] is True and resumed["id"] == job_id
                assert resumed["payload"] == original["payload"] and resumed["model"] == original["model"]
                assert "execution_token" not in resumed
                await second.send(json.dumps({"type": "done", "id": job_id, "results": [{"index": 0, "sha256": "a" * 64, "seed": 31}]}))
                ack = await frame(second, "ack")
                assert ack["id"] == job_id
            recovered = await client.get(created.headers["location"])
            assert recovered.status_code == 200 and recovered.json()["state"] == "completed", recovered.text
            replay = await client.post("/v1/videos/jobs", headers={"Idempotency-Key": idempotency}, json=body)
            assert replay.status_code == 200 and replay.json() == recovered.json()
            async with engine.connect() as connection:
                for table in (tables.media_jobs, tables.reservations, tables.ledger):
                    assert await connection.scalar(sa.select(sa.func.count()).select_from(table)) == 1
                assert (
                    await connection.scalar(
                        sa.select(sa.func.count()).select_from(tables.credit_ledger).where(tables.credit_ledger.c.ref == job_id),
                    )
                    == 1
                )
                assert await connection.scalar(sa.select(tables.media_jobs.c.execution_worker_id)) == UUID(worker_id)
                assert await connection.scalar(sa.select(tables.ledger.c.worker_id)) == UUID(worker_id)
            # No second execution is ever placed on the recovered queue.
            assert await redis.xlen("grid:jobs:media") == 0
        finally:
            await second.close()
