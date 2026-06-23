# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Prometheus /metrics endpoint. Reads counters from services.metrics_state."""

from fastapi import APIRouter, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from ..redis_client import STREAM_KEY, get_redis, WORKER_ACTIVE_SET_KEY
from ..services.metrics_state import WORKERS_CONNECTED, QUEUE_DEPTH, MODELS_AVAILABLE

router = APIRouter()


@router.get("/metrics")
async def metrics():
    """Prometheus metrics in text exposition format."""
    try:
        r = get_redis()
        # Worker count from Redis set (use the canonical constant — was hardcoded, which
        # silently mismatches if the key ever changes in redis_client).
        workers = await r.scard(WORKER_ACTIVE_SET_KEY)
        WORKERS_CONNECTED.set(workers)

        # Queue depth
        depth = await r.xlen(STREAM_KEY)
        QUEUE_DEPTH.set(depth)

        # Distinct models from worker status keys — pipelined (one round-trip).
        worker_ids = list(await r.smembers(WORKER_ACTIVE_SET_KEY))
        models = set()
        if worker_ids:
            import json
            pipe = r.pipeline()
            for wid in worker_ids:
                pipe.get(f"grid:worker:{wid}:status")
            for data in await pipe.execute():
                if data:
                    models.update(json.loads(data).get("models", []))
        MODELS_AVAILABLE.set(len(models))
    except Exception:
        pass

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
