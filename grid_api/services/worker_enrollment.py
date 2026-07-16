# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Short-lived device enrollment for native worker managers.

The manager creates the final API credential locally and sends it once over
TLS. Core hashes it immediately and retains only the hash. A human with a
recent Google/SIWE Core session prepares and signs the payout-wallet delegation.
The key remains short-lived until the manager retrieves, verifies, and
acknowledges that certificate.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from uuid import UUID

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import is_address

from ..auth import hash_api_key
from ..config import get_settings
from ..redis_client import get_redis
from . import accounts
from .worker_identity import delegation_message

STATE_VERSION = 1
KEY_PREFIX = "grid:worker-enrollment:"
LOCK_PREFIX = "grid:worker-enrollment-lock:"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,80}$")
HEX_32_RE = re.compile(r"^[0-9a-f]{64}$")
API_KEY_RE = re.compile(r"^grid_[A-Za-z0-9_-]{32,64}$")
MAX_WORKER_NAME = 120
MAX_PROFILE_ID = 128


class EnrollmentError(ValueError):
    """The enrollment is missing, stale, malformed, or in the wrong state."""


class EnrollmentConflict(EnrollmentError):
    """Enrollment state conflicts with the requested account or wallet."""


class EnrollmentUnauthorized(EnrollmentError):
    """A manager poll token or wallet signature is invalid."""


def _now() -> int:
    return int(time.time())


def _state_key(enrollment_id: str) -> str:
    if not ID_RE.fullmatch(enrollment_id):
        raise EnrollmentError("invalid worker enrollment id")
    return f"{KEY_PREFIX}{enrollment_id}"


def _poll_hash(token: str) -> str:
    if not isinstance(token, str) or not 32 <= len(token) <= 200:
        raise EnrollmentUnauthorized("invalid worker enrollment poll token")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _clean_create(
    *,
    worker_signer: str,
    worker_name: str,
    profile_id: str,
    api_key: str,
    poll_token_hash: str,
    valid_days: int,
) -> dict[str, Any]:
    if not is_address(worker_signer):
        raise EnrollmentError("worker signer is not an address")
    name = worker_name.strip()
    if not name or name != worker_name or len(name) > MAX_WORKER_NAME:
        raise EnrollmentError("worker name must be trimmed and at most 120 characters")
    profile = profile_id.strip()
    if not profile or profile != profile_id or len(profile) > MAX_PROFILE_ID:
        raise EnrollmentError("profile id must be trimmed and at most 128 characters")
    if not API_KEY_RE.fullmatch(api_key):
        raise EnrollmentError("candidate worker credential has an invalid format")
    if not HEX_32_RE.fullmatch(poll_token_hash):
        raise EnrollmentError("poll token commitment must be lowercase SHA-256 hex")
    if not 1 <= valid_days <= 365:
        raise EnrollmentError("delegation validity must be between 1 and 365 days")
    return {
        "worker_signer": worker_signer.lower(),
        "worker_name": name,
        "profile_id": profile,
        "api_key_hash": hash_api_key(api_key),
        "poll_token_hash": poll_token_hash,
        "valid_days": valid_days,
    }


async def create_enrollment(**values) -> Mapping[str, Any]:
    clean = _clean_create(**values)
    settings = get_settings()
    ttl = max(300, min(int(settings.worker_enrollment_ttl_seconds), 1800))
    redis = get_redis()
    for _ in range(5):
        enrollment_id = secrets.token_urlsafe(24)
        created = _now()
        state = {
            "state_version": STATE_VERSION,
            "enrollment_id": enrollment_id,
            "status": "pending",
            "created_at": created,
            "expires_at": created + ttl,
            "delegation_id": secrets.token_hex(16),
            **clean,
        }
        if await redis.set(
            _state_key(enrollment_id),
            json.dumps(state, separators=(",", ":")),
            nx=True,
            ex=ttl,
        ):
            base = settings.worker_enrollment_console_url.rstrip("/")
            return {
                "enrollment_id": enrollment_id,
                "authorize_url": f"{base}/{quote(enrollment_id, safe='')}",
                "expires_at": state["expires_at"],
                "poll_after_seconds": 2,
            }
    raise EnrollmentError("could not allocate a worker enrollment id")


async def public_enrollment(enrollment_id: str) -> Mapping[str, Any]:
    state = await _load(enrollment_id)
    return {
        "enrollment_id": state["enrollment_id"],
        "status": state["status"],
        "worker_signer": state["worker_signer"],
        "worker_name": state["worker_name"],
        "profile_id": state["profile_id"],
        "expires_at": state["expires_at"],
        "chain_id": get_settings().worker_identity_chain_id,
        "audience": get_settings().worker_identity_audience,
    }


async def prepare_enrollment(
    enrollment_id: str,
    *,
    user: Mapping[str, Any],
    payout_wallet: str,
    replace_payout_wallet: bool,
) -> Mapping[str, Any]:
    wallet = _address(payout_wallet, "payout wallet")
    async with _enrollment_lock(enrollment_id):
        state = await _load(enrollment_id)
        if state["status"] not in {"pending", "prepared"}:
            raise EnrollmentConflict("worker enrollment is already completed")
        account_id = str(user.get("account_id") or "")
        if not account_id:
            raise EnrollmentUnauthorized("worker enrollment needs a v2 account")
        existing = str(user.get("payout_wallet") or user.get("wallet") or "").lower()
        if existing and existing != wallet and not replace_payout_wallet:
            raise EnrollmentConflict(
                "this account already has a different payout wallet; explicit replacement is required",
            )
        if state["status"] == "prepared":
            if state.get("account_id") != account_id:
                raise EnrollmentConflict("worker enrollment was prepared by another account")
            payload = state.get("delegation_payload") or {}
            if payload.get("payout_wallet") != wallet:
                raise EnrollmentConflict("worker enrollment was prepared for another wallet")
            return {"message": delegation_message(payload), "payload": payload}

        issued = _now()
        settings = get_settings()
        payload = {
            "version": 1,
            "chain_id": settings.worker_identity_chain_id,
            "audience": settings.worker_identity_audience.lower(),
            "delegation_id": state["delegation_id"],
            "payout_wallet": wallet,
            "worker_signer": state["worker_signer"],
            "worker_name": state["worker_name"],
            "issued_at": issued,
            "expires_at": issued + int(state["valid_days"]) * 86400,
        }
        state.update(
            {
                "status": "prepared",
                "account_id": account_id,
                "replace_payout_wallet": bool(replace_payout_wallet),
                "delegation_payload": payload,
            },
        )
        await _save_keep_ttl(state)
        return {"message": delegation_message(payload), "payload": payload}


async def approve_enrollment(
    enrollment_id: str,
    *,
    user: Mapping[str, Any],
    signature: str,
) -> Mapping[str, Any]:
    async with _enrollment_lock(enrollment_id):
        state = await _load(enrollment_id)
        if state["status"] in {"complete", "activated"}:
            return {"status": state["status"]}
        if state["status"] != "prepared":
            raise EnrollmentConflict("worker enrollment must be prepared before approval")
        if state.get("account_id") != str(user.get("account_id") or ""):
            raise EnrollmentUnauthorized("worker enrollment belongs to another account")
        payload = state["delegation_payload"]
        try:
            recovered = Account.recover_message(
                encode_defunct(text=delegation_message(payload)),
                signature=signature,
            ).lower()
        except Exception as exc:
            raise EnrollmentUnauthorized("invalid payout-wallet delegation signature") from exc
        if recovered != payload["payout_wallet"]:
            raise EnrollmentUnauthorized("delegation was not signed by the payout wallet")

        account_id = UUID(state["account_id"])
        temporary_expiry = datetime.now(timezone.utc) + timedelta(
            seconds=max(300, min(get_settings().worker_enrollment_ttl_seconds, 1800)),
        )
        key_installed = False
        try:
            await accounts.install_enrolled_worker_key(
                account_id,
                state["api_key_hash"],
                label=f"worker:{state['worker_name']}",
                expires_at=temporary_expiry,
                payout_wallet=recovered,
            )
            key_installed = True
            state.update(
                {
                    "status": "complete",
                    "certificate": {"payload": payload, "signature": signature},
                    "completed_at": _now(),
                },
            )
            await get_redis().set(
                _state_key(enrollment_id),
                json.dumps(state, separators=(",", ":")),
                ex=max(300, min(get_settings().worker_enrollment_ttl_seconds, 1800)),
            )
        except Exception:
            if key_installed:
                await accounts.revoke_prehashed_worker_key(
                    account_id,
                    state["api_key_hash"],
                )
            raise
        return {"status": "complete"}


async def poll_enrollment(
    enrollment_id: str,
    *,
    poll_token: str,
) -> Mapping[str, Any]:
    state = await _load(enrollment_id)
    _require_poll_token(state, poll_token)
    result: dict[str, Any] = {"status": state["status"]}
    if state["status"] in {"complete", "activated"}:
        result["certificate"] = state["certificate"]
        result["worker_name"] = state["worker_name"]
    return result


async def acknowledge_enrollment(
    enrollment_id: str,
    *,
    poll_token: str,
) -> Mapping[str, Any]:
    async with _enrollment_lock(enrollment_id):
        state = await _load(enrollment_id)
        _require_poll_token(state, poll_token)
        if state["status"] == "activated":
            return {"status": "activated"}
        if state["status"] != "complete":
            raise EnrollmentConflict("worker enrollment is not ready to activate")
        activated = await accounts.activate_prehashed_worker_key(
            UUID(state["account_id"]),
            state["api_key_hash"],
        )
        if not activated:
            raise EnrollmentError("temporary worker credential is missing")
        state["status"] = "activated"
        state["activated_at"] = _now()
        await get_redis().set(
            _state_key(enrollment_id),
            json.dumps(state, separators=(",", ":")),
            ex=60,
        )
        return {"status": "activated"}


def _require_poll_token(state: Mapping[str, Any], token: str) -> None:
    supplied = _poll_hash(token)
    expected = str(state.get("poll_token_hash") or "")
    if not secrets.compare_digest(supplied, expected):
        raise EnrollmentUnauthorized("invalid worker enrollment poll token")


def _address(value: Any, label: str) -> str:
    if not isinstance(value, str) or not is_address(value):
        raise EnrollmentError(f"{label} is not an address")
    return value.lower()


async def _load(enrollment_id: str) -> dict[str, Any]:
    raw = await get_redis().get(_state_key(enrollment_id))
    if not raw:
        raise EnrollmentError("worker enrollment was not found or has expired")
    try:
        state = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise EnrollmentError("worker enrollment state is corrupted") from exc
    if state.get("state_version") != STATE_VERSION:
        raise EnrollmentError("worker enrollment state version is unsupported")
    return state


async def _save_keep_ttl(state: Mapping[str, Any]) -> None:
    key = _state_key(str(state["enrollment_id"]))
    redis = get_redis()
    ttl = await redis.ttl(key)
    if ttl <= 0:
        raise EnrollmentError("worker enrollment has expired")
    await redis.set(key, json.dumps(state, separators=(",", ":")), ex=ttl)


class _enrollment_lock:
    def __init__(self, enrollment_id: str):
        self.key = f"{LOCK_PREFIX}{_state_key(enrollment_id)[len(KEY_PREFIX):]}"
        self.token = secrets.token_urlsafe(24)

    async def __aenter__(self):
        acquired = await get_redis().set(self.key, self.token, nx=True, ex=30)
        if not acquired:
            raise EnrollmentConflict("worker enrollment is already being updated")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        redis = get_redis()
        try:
            await redis.eval(
                "if redis.call('get',KEYS[1])==ARGV[1] then " "return redis.call('del',KEYS[1]) else return 0 end",
                1,
                self.key,
                self.token,
            )
        except Exception:
            if await redis.get(self.key) == self.token:
                await redis.delete(self.key)
