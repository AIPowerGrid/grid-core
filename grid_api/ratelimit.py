# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared rate limiter for the Grid API.

One limiter instance, imported everywhere. Two upgrades over the previous
per-module, in-memory, per-IP limiters:

  1. Keyed by API KEY, not IP. The old per-IP limiter punished everyone
     behind a shared NAT/proxy with a single bucket (and, behind the proxy,
     often saw only the proxy's IP — so one bucket for the whole world).
     Per-key means one user's burst can't 429 everyone else.

  2. Redis-backed storage. In-memory limits were per-uvicorn-worker (so the
     real limit was Nx the configured value) and reset on every restart.
     Shared Redis storage makes the limit real and durable, with an
     in-memory fallback so a Redis blip doesn't take the API down.

Unauthenticated requests fall back to per-IP limiting.
"""

import logging

from slowapi import Limiter
from slowapi.util import get_remote_address

from .auth import hash_api_key
from .config import get_settings

logger = logging.getLogger("grid_api.ratelimit")


def _api_key_or_ip(request) -> str:
    """Rate-limit bucket key: the caller's API key if present, else their IP.

    Hash the key so raw secrets never become Redis key names.
    """
    apikey = request.headers.get("apikey")
    auth = request.headers.get("authorization")
    raw = None
    if apikey:
        raw = apikey
    elif auth:
        raw = auth[7:] if auth.lower().startswith("bearer ") else auth
    if raw:
        return "k:" + hash_api_key(raw)
    return "ip:" + get_remote_address(request)


_settings = get_settings()

limiter = Limiter(
    key_func=_api_key_or_ip,
    storage_uri=_settings.redis_url,
    # If Redis is unreachable, degrade to in-memory limiting instead of
    # erroring every request. Limits become per-process in that window, but
    # the API keeps serving.
    in_memory_fallback_enabled=True,
    strategy="fixed-window",
)
