# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from fastapi import APIRouter

from ..redis_client import get_redis
from .worker_ws import get_available_models, get_connected_worker_count

router = APIRouter()


@router.get("/health")
async def health():
    """Health check — Redis connectivity + worker status."""
    redis_ok = False
    try:
        r = get_redis()
        await r.ping()
        redis_ok = True
    except Exception:
        pass

    models = await get_available_models()
    workers = await get_connected_worker_count()
    return {
        "status": "ok" if redis_ok else "degraded",
        "redis": redis_ok,
        "workers_connected": workers,
        "models_available": models,
    }
