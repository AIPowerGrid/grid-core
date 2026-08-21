# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Data-minimized identifiers and exception metadata for operational logs."""

from __future__ import annotations

import hashlib
from functools import lru_cache

from .auth import _get_api_key_salt


@lru_cache(maxsize=1)
def _log_key() -> bytes:
    """Derive a domain-separated logging key from the server-only API salt."""
    return hashlib.blake2b(
        _get_api_key_salt().encode("utf-8"),
        digest_size=32,
        person=b"aipg-log-key-v1",
    ).digest()


def opaque_id(value) -> str:
    """Return a stable, keyed identifier suitable for log correlation."""
    if value in (None, ""):
        return "-"
    return hashlib.blake2b(
        str(value).encode("utf-8"),
        key=_log_key(),
        digest_size=9,
        person=b"aipg-log-id-v1",
    ).hexdigest()


def error_type(exc: BaseException) -> str:
    """Expose only a bounded exception class name, never its message or values."""
    return type(exc).__name__[:80] or "Exception"
