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
        # Worker count from Redis set
        workers = await r.scard("grid:workers:active")
        WORKERS_CONNECTED.set(workers)

        # Queue depth
        depth = await r.xlen(STREAM_KEY)
        QUEUE_DEPTH.set(depth)

        # Count distinct models from worker status keys
        worker_ids = await r.smembers("grid:workers:active")
        models = set()
        for wid in worker_ids:
            data = await r.get(f"grid:worker:{wid}:status")
            if data:
                import json
                info = json.loads(data)
                models.update(info.get("models", []))
        MODELS_AVAILABLE.set(len(models))
    except Exception:
        pass

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
