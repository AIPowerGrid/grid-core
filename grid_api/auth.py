# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Authentication for Grid API — uses the same hash as Flask.

We reimplement hash_api_key here to avoid importing horde.utils, which
pulls in the entire Flask app via its import chain.
"""

import hashlib
from typing import Optional

import sqlalchemy as sa
from fastapi import Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_session, users_table


def hash_api_key(unhashed_api_key: str) -> str:
    """SHA256 hash with the same hardcoded salt as the Flask app.

    Must produce identical output to horde/utils.py:hash_api_key().
    """
    salt = "s0m3s3cr3t"
    return hashlib.sha256(salt.encode() + unhashed_api_key.encode()).hexdigest()


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


async def get_current_user(
    apikey: str = Header(..., description="API key for authentication"),
    session: AsyncSession = None,
):
    """Validate an API key and return the user row.

    Used as a FastAPI dependency. Hashes the key with the same SHA256+salt
    as the Flask app so both systems share the same user accounts.
    """
    if not apikey:
        raise HTTPException(status_code=401, detail="Missing apikey header")

    hashed = hash_api_key(apikey)
    result = await session.execute(
        sa.select(users_table).where(users_table.c.api_key == hashed)
    )
    user = result.mappings().first()

    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return dict(user)
