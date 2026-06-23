# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""AI Power Grid — Streaming API Server.

Runs alongside the Flask API on a separate port. Provides:
  - POST /v1/chat/completions  (OpenAI-compatible, streaming)
  - POST /v1/messages          (Anthropic-compatible, streaming)
  - POST /v1/images/generations (OpenAI-compatible image gen)
  - GET  /v1/models            (available models from connected workers)
  - WS   /v1/workers/ws        (WebSocket for text generation workers)
  - GET  /health               (health check)
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from .database import close_database, init_database
from .ratelimit import limiter
from .redis_client import close_redis, init_redis
from .routers import accounts, anthropic, health, images, metrics, openai, responses, stats, worker_ws
from .services.p2p import init_p2p, close_p2p

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("grid_api")

# Rate limiter is the shared, Redis-backed, per-API-key limiter from
# .ratelimit (imported above). All routers use the same instance.


async def _stale_job_reclaimer():
    """Background task: periodically reclaim abandoned jobs."""
    from .services.job_queue import claim_stale_jobs
    while True:
        try:
            reclaimed = await claim_stale_jobs()
            if reclaimed:
                logger.info(f"Reclaimed {reclaimed} stale jobs")
        except Exception as e:
            logger.error(f"Stale job reclaimer error: {e}")
        await asyncio.sleep(60)  # Check every minute


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    logger.info("Starting Grid Streaming API...")
    await init_database()
    await init_redis()
    await init_p2p()  # Initialize P2P (no-op if disabled)
    reclaimer = asyncio.create_task(_stale_job_reclaimer())
    logger.info("Grid Streaming API ready.")
    yield
    logger.info("Shutting down Grid Streaming API...")
    reclaimer.cancel()
    await close_p2p()  # Shutdown P2P
    await close_redis()
    await close_database()


app = FastAPI(
    title="AI Power Grid — Streaming API",
    description="OpenAI and Anthropic compatible endpoints with real token streaming.",
    version="1.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"error": {"message": "Rate limit exceeded. Please slow down.", "type": "rate_limit_error"}},
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(openai.router)
app.include_router(anthropic.router)
app.include_router(responses.router)
app.include_router(images.router)
app.include_router(worker_ws.router)
app.include_router(stats.router)
app.include_router(accounts.router)
app.include_router(health.router)
app.include_router(metrics.router)


@app.get("/")
async def root():
    from .services.p2p import get_p2p_node, get_p2p_config

    p2p_config = get_p2p_config()
    p2p_node = get_p2p_node()

    return {
        "name": "AI Power Grid — Streaming API",
        "version": "1.0.0",
        "endpoints": {
            "openai": "POST /v1/chat/completions",
            "anthropic": "POST /v1/messages",
            "images": "POST /v1/images/generations",
            "models": "GET /v1/models",
            "worker_ws": "WS /v1/workers/ws",
            "health": "GET /health",
        },
        "p2p": {
            "enabled": p2p_config.enabled,
            "peer_id": p2p_node.peer_id if p2p_node else None,
            "status": "running" if p2p_node and p2p_node.running else "disabled",
        },
    }
