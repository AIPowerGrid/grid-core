# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Authentication for Grid API — uses the same hash as Flask.

We reimplement hash_api_key here to avoid importing horde.utils, which
pulls in the entire Flask app via its import chain.
"""

import hashlib
import os
from typing import Optional

from fastapi import HTTPException

_API_KEY_SALT = None


def _get_api_key_salt() -> str:
    """Read the API-key salt from GRID_SALT, once, failing loudly.

    Must match horde/utils.py and the dashboard's generate-api-key route —
    all three read the same GRID_SALT so keys hash identically everywhere.
    Refuses to run unset or with the known-compromised legacy value (see
    horde/utils.py for the history).
    """
    global _API_KEY_SALT
    if _API_KEY_SALT is None:
        salt = os.getenv("GRID_SALT")
        if not salt:
            raise RuntimeError(
                "GRID_SALT is not set. Refusing to hash API keys without a real "
                "secret — set GRID_SALT in the environment (see deploy/env.template)."
            )
        if salt == "s0m3s3cr3t":
            raise RuntimeError(
                "GRID_SALT is set to the known-compromised legacy value. "
                "Generate a fresh secret (e.g. `openssl rand -hex 32`)."
            )
        _API_KEY_SALT = salt
    return _API_KEY_SALT


def hash_api_key(unhashed_api_key: str) -> str:
    """SHA256(salt + key). Salt comes from GRID_SALT — same as the Flask app
    and the dashboard, so all systems share one user table."""
    return hashlib.sha256(_get_api_key_salt().encode() + unhashed_api_key.encode()).hexdigest()


def extract_api_key(
    apikey: Optional[str] = None,
    authorization: Optional[str] = None,
) -> str:
    """Extract API key from either `apikey` header or `Authorization: Bearer` header.

    Supports both Grid-native auth (apikey header) and OpenAI-compatible auth
    (Authorization: Bearer). This lets Portkey gateway and OpenAI SDKs work natively.
    """
    if apikey:
        return apikey
    if authorization:
        if authorization.startswith("Bearer "):
            return authorization[7:]
        return authorization
    raise HTTPException(status_code=401, detail="Missing API key. Use 'apikey' header or 'Authorization: Bearer' header.")
