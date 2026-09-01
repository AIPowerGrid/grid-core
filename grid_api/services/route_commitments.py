# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Domain-separated commitments for private route-observer correlation."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Sequence


def _commit(domain: str, parts: Sequence[str], *, secret: str) -> str:
    if not secret or not parts or any(not part for part in parts):
        raise ValueError("commitment secret and material are required")
    payload = json.dumps(
        {"domain": domain, "parts": list(parts)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def job_ref(job_id: str, *, secret: str) -> str:
    """Return a stable private reference shared by every attempt of one job."""
    return _commit("job.v1", (str(job_id),), secret=secret)


def route_ref(job_id: str, stream: str, stream_id: str, *, secret: str) -> str:
    """Return a private reference unique to one queue-delivery attempt."""
    if not stream_id:
        raise ValueError("route stream id is required")
    return _commit("route.v1", (str(job_id), str(stream), str(stream_id)), secret=secret)
