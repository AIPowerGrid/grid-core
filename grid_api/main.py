# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""AI Power Grid coordinator and public API.

This is the sole production coordinator runtime. Provides:
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
from .routers import (
    accounts,
    anthropic,
    audio,
    health,
    images,
    metrics,
    oauth,
    openai,
    responses,
    stats,
    styles,
    threed,
    validator,
    validator_pairing,
    videos,
    worker_enrollment,
    worker_ws,
)
from .services import x402_payments
from .services.p2p import close_p2p, init_p2p

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
            from .services import alerts

            alerts.emit(
                "stale_job_reclaimer_failed",
                "critical",
                "The background queue reclaimer failed.",
                fields={"error_type": type(e).__name__},
                dedupe_key="stale-job-reclaimer-failed",
            )
        await asyncio.sleep(60)  # Check every minute


async def _reservation_sweeper():
    """Background task: release reservations stuck in 'held' past a generous
    deadline — the safety net for a process crash between reserve and the
    worker-WS terminal. No-op while charging is dark. Interval/threshold via
    RESERVATION_SWEEP_SECONDS / RESERVATION_STALE_SECONDS."""
    import os
    from .services.credits import sweep_stale_reservations
    from .services.promotions import sweep_stale_spends
    from .services.validator_audit_budgets import sweep_expired_audits
    interval = int(os.getenv("RESERVATION_SWEEP_SECONDS", "300") or 300)
    stale = int(os.getenv("RESERVATION_STALE_SECONDS", "3600") or 3600)
    while True:
        try:
            await sweep_stale_reservations(older_than_seconds=stale)
            await sweep_stale_spends(older_than_seconds=stale)
            audit_recovery = await sweep_expired_audits()
            if audit_recovery["released"] or audit_recovery["manual_review"]:
                logger.warning(
                    "Recovered expired compensated-audit holds released=%d manual_review=%d",
                    audit_recovery["released"],
                    audit_recovery["manual_review"],
                )
        except Exception as e:
            logger.error(f"Reservation sweeper error: {e}")
            from .services import alerts

            alerts.emit(
                "reservation_sweeper_failed",
                "critical",
                "The stale monetary-hold recovery loop failed.",
                fields={"error_type": type(e).__name__},
                dedupe_key="reservation-sweeper-failed",
            )
        await asyncio.sleep(interval)


async def _validator_history_sweeper():
    """Bound finalized validator assignment/group storage.

    Signed attestations remain append-only; only the operational rows that held
    leases and private challenges are removed after the public health window.
    """
    from .config import get_settings
    from .safe_logging import error_type
    from .services.validators import prune_validator_operational_history

    interval = max(3600, get_settings().validator_history_sweep_seconds)
    while True:
        try:
            deleted = await prune_validator_operational_history()
            if deleted["assignments"] or deleted["probe_groups"]:
                logger.info(
                    "Pruned finalized validator rows assignments=%d probe_groups=%d",
                    deleted["assignments"],
                    deleted["probe_groups"],
                )
        except Exception as exc:
            logger.error(
                "Validator history sweeper error_type=%s",
                error_type(exc),
            )
        await asyncio.sleep(interval)


async def _validator_bond_sync_loop():
    """Refresh the dark media-reference bond cache from finalized Base state."""
    from .config import get_settings
    from .safe_logging import error_type
    from .services import alerts
    from .services.validator_bonds import sync_reference_bonds_once

    settings = get_settings()
    interval = max(60, settings.validator_media_bond_sync_seconds)
    while True:
        try:
            result = await sync_reference_bonds_once(settings=settings)
            if result["status"] == "synced":
                logger.info(
                    "Validator bond cache refreshed rows=%d inactive=%d block=%d",
                    result["updated"],
                    result["inactive"],
                    result["finalized_block"],
                )
            elif result["status"] == "faulted":
                alerts.emit(
                    "validator_bond_sync_faulted",
                    "critical",
                    "Finalized worker-bond evidence was invalidated after a sync fault.",
                    fields={"invalidated": result["invalidated"]},
                    dedupe_key="validator-bond-sync-faulted",
                )
        except Exception as exc:
            logger.error("Validator bond sync error_type=%s", error_type(exc))
            alerts.emit(
                "validator_bond_sync_failed",
                "warning",
                "The finalized worker-bond cache refresh failed.",
                fields={"error_type": error_type(exc)},
                dedupe_key="validator-bond-sync-failed",
            )
        await asyncio.sleep(interval)


async def _recipe_sync_loop():
    """Refresh a dual-RPC verified RecipeVault snapshot off the hot path."""
    from .config import get_settings
    from .safe_logging import error_type
    from .services.recipes import sync_from_recipevault

    interval = max(60, get_settings().recipe_sync_seconds)
    while True:
        try:
            await sync_from_recipevault()
        except Exception as exc:
            logger.error("Recipe sync loop error_type=%s", error_type(exc))
        await asyncio.sleep(interval)


async def _billing_monitor():
    """Periodically prove the cached purchased balance matches its ledger."""
    import os

    from .services import alerts
    from .services.credits import billing_health

    interval = max(60, int(os.getenv("GRID_BILLING_MONITOR_SECONDS", "300") or 300))
    held_warning = int(os.getenv("GRID_BILLING_HELD_WARNING_SECONDS", "900") or 900)
    while True:
        try:
            health = await billing_health(held_warning_seconds=held_warning)
            if not health["ok"]:
                alerts.emit(
                    "billing_invariant_failed",
                    "critical",
                    "The purchased-credit cache no longer matches its append-only ledger.",
                    fields=health,
                    dedupe_key="billing-invariant-failed",
                )
            if health["stale_held"]:
                alerts.emit(
                    "billing_holds_aging",
                    "warning",
                    "Monetary reservations are still held beyond the warning threshold.",
                    fields={
                        "held_count": health["stale_held"],
                        "warning_age_seconds": held_warning,
                    },
                    dedupe_key="billing-holds-aging",
                )
            await x402_payments.verify_reported_settlements()
            await x402_payments.flag_stale_settlements()
        except Exception as exc:
            logger.error("Billing invariant monitor error: %s", exc)
            alerts.emit(
                "billing_monitor_failed",
                "critical",
                "The read-only billing invariant monitor failed.",
                fields={"error_type": type(exc).__name__},
                dedupe_key="billing-monitor-failed",
            )
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    from .services import alerts

    logger.info("Starting Grid Streaming API...")
    await alerts.start()
    try:
        await init_database()
        await init_redis()
        await init_p2p()  # Initialize P2P (no-op if disabled)
    except Exception as exc:
        alerts.emit(
            "core_startup_failed",
            "critical",
            "Grid Core failed during dependency initialization.",
            fields={"error_type": type(exc).__name__},
            dedupe_key="core-startup-failed",
        )
        await alerts.flush()
        await alerts.stop()
        raise
    reclaimer = asyncio.create_task(_stale_job_reclaimer())
    # Media recipes: load curated local recipes now. Chain sync is an explicit,
    # default-off, dual-RPC verified background authority.
    import os

    from .services import loras as _loras
    from .services import recipes as _recipes
    from .services import styles as _styles
    _base = os.path.dirname(os.path.dirname(__file__))
    _recipes.load_local_recipes(os.path.join(_base, "recipes"))
    _loras.load_blacklist(os.path.join(_base, "lora_blacklist.json"))
    # Styles: curated creative presets that compose over recipes. Served at
    # /v1/styles and applied server-side when a request carries `style`.
    _styles.load_local_styles(os.path.join(_base, "styles"))
    recipe_sync = asyncio.create_task(_recipe_sync_loop())
    sweeper = asyncio.create_task(_reservation_sweeper())
    validator_history_sweeper = asyncio.create_task(_validator_history_sweeper())
    validator_bond_sync = asyncio.create_task(_validator_bond_sync_loop())
    billing_monitor = asyncio.create_task(_billing_monitor())
    # Verification probes ("validator zero") — dormant unless GRID_PROBE_ENABLED;
    # even ON it only records evidence (no reward/slash). See VERIFICATION_PROBES.md.
    from .services import probe as _probe
    prober = asyncio.create_task(_probe.probe_loop())
    from .services import credits as _credits
    alerts.emit(
        "core_started",
        "success",
        "Grid Core started and its background safety loops are running.",
        fields={
            "charging_mode": _credits.charging_mode(),
            "charging_accounts": len(_credits.CHARGING_ALLOW_ACCOUNTS),
            "charging_services": len(_credits.CHARGING_ALLOW_SERVICES),
            "charging_models": len(_credits.CHARGING_ALLOW_MODELS),
        },
        dedupe_key="core-started",
    )
    logger.info("Grid Streaming API ready.")
    yield
    logger.info("Shutting down Grid Streaming API...")
    reclaimer.cancel()
    recipe_sync.cancel()
    sweeper.cancel()
    validator_history_sweeper.cancel()
    validator_bond_sync.cancel()
    billing_monitor.cancel()
    prober.cancel()
    await close_p2p()  # Shutdown P2P
    await close_redis()
    await close_database()
    await alerts.stop()


app = FastAPI(
    title="AI Power Grid — Streaming API",
    description="OpenAI and Anthropic compatible endpoints with real token streaming.",
    version="1.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter


@app.middleware("http")
async def alert_unhandled_http_failure(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        from .services import alerts

        alerts.emit(
            "unhandled_http_failure",
            "critical",
            "A Core HTTP request failed with an unhandled server exception.",
            fields={
                "method": request.method,
                "path": request.url.path,
                "error_type": type(exc).__name__,
            },
            dedupe_key=f"http-500:{request.method}:{request.url.path}:{type(exc).__name__}",
        )
        raise


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    from .services import alerts

    alerts.emit(
        "http_rate_limited",
        "warning",
        "A public API route is being rate limited.",
        fields={"method": request.method, "path": request.url.path},
        dedupe_key=f"http-429:{request.method}:{request.url.path}",
    )
    return JSONResponse(
        status_code=429,
        content={"error": {"message": "Rate limit exceeded. Please slow down.", "type": "rate_limit_error"}},
    )


x402_payments.install_middleware(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # Auth is via the Authorization/apikey HEADER, never cookies — so credentials must be
    # FALSE. "*" + allow_credentials=True is a footgun: Starlette then reflects the caller's
    # Origin AND sets Allow-Credentials:true, making every origin a trusted credentialed one.
    # With credentials off, the public API stays callable cross-origin (header auth works)
    # without that reflection. Authorization is allowed via allow_headers below.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(openai.router)
app.include_router(oauth.router)
app.include_router(anthropic.router)
app.include_router(responses.router)
app.include_router(images.router)
app.include_router(videos.router)
app.include_router(audio.router)
app.include_router(threed.router)
app.include_router(worker_ws.router)
app.include_router(worker_enrollment.router)
app.include_router(stats.router)
app.include_router(styles.router)
app.include_router(validator.router)
app.include_router(validator_pairing.router)
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
            "audio": "POST /v1/audio/generations",
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
