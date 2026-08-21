# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import re
from pathlib import Path

from fastapi import APIRouter

from ..redis_client import get_redis
from .worker_ws import get_available_models, get_connected_worker_count

router = APIRouter()

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_GIT_HEAD = Path(__file__).resolve().parents[2] / ".git" / "HEAD"


def build_commit() -> str | None:
    """Return the immutable release commit without spawning git.

    Production may set ``GRID_BUILD_COMMIT`` explicitly. Immutable source
    checkouts also expose a detached ``.git/HEAD`` containing the same full
    SHA. Source archives and developer checkouts degrade to ``None`` rather
    than reporting an unverified branch name.
    """
    configured = os.getenv("GRID_BUILD_COMMIT", "").strip().lower()
    if _COMMIT_RE.fullmatch(configured):
        return configured
    try:
        head = _GIT_HEAD.read_text(encoding="ascii").strip().lower()
    except OSError:
        return None
    return head if _COMMIT_RE.fullmatch(head) else None


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
        "build_commit": build_commit(),
        "redis": redis_ok,
        "workers_connected": workers,
        "models_available": models,
    }
