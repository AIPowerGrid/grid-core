# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Verify payout-wallet delegation and fresh worker registration proofs."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_utils import is_address

from ..config import get_settings
from ..redis_client import get_redis
from . import audio

IDENTITY_VERSION = 1
DELEGATION_DOMAIN = "aipg-worker-delegation"
REGISTRATION_DOMAIN = "aipg-worker-registration"
NONCE_PREFIX = "grid:worker-registration:nonce:"
MAX_DELEGATION_SECONDS = 365 * 86400
DELEGATION_FIELDS = frozenset(
    {
        "version",
        "chain_id",
        "audience",
        "delegation_id",
        "payout_wallet",
        "worker_signer",
        "worker_name",
        "issued_at",
        "expires_at",
    },
)
REGISTRATION_FIELDS = frozenset(
    {
        "version",
        "timestamp",
        "nonce",
        "worker_signer",
        "worker_name",
        "models",
        "job_types",
        "bridge_agent",
        "profile_digest",
        "profile_recipe_root",
    },
)
PROFILE_FIELDS = frozenset(
    {
        "id",
        "version",
        "digest",
        "signing_key_id",
        "capability_tier",
        "canary_completed_at",
        "canary_elapsed_seconds",
        "runtime_adapter",
        "runtime_digest",
        "recipe_root",
    },
)


class WorkerIdentityError(ValueError):
    """A worker identity proof is missing, malformed, stale, or unauthorized."""


@dataclass(frozen=True)
class VerifiedWorkerIdentity:
    signer_address: str
    payout_wallet: str
    delegation_id: str
    expires_at: int


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def delegation_message(payload: Mapping[str, Any]) -> str:
    return f"{DELEGATION_DOMAIN}:v1:{canonical_json(payload)}"


def registration_message(payload: Mapping[str, Any]) -> str:
    return f"{REGISTRATION_DOMAIN}:v1:{canonical_json(payload)}"


def normalize_worker_profile(value: Any) -> dict[str, Any] | None:
    """Accept only the privacy-safe profile fields understood by Core."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != PROFILE_FIELDS:
        raise WorkerIdentityError("worker_profile has unsupported or missing fields")
    for field in ("id", "version", "signing_key_id", "capability_tier", "canary_completed_at"):
        if not isinstance(value[field], str) or not value[field] or len(value[field]) > 200:
            raise WorkerIdentityError(f"worker_profile.{field} is invalid")
    digest = value["digest"]
    if not _is_hex(digest, 64):
        raise WorkerIdentityError("worker_profile.digest must be a SHA-256 hex digest")
    if value["id"] != audio.DEFAULT_AUDIO_MODEL:
        raise WorkerIdentityError("worker_profile.id is unsupported")
    if value["runtime_adapter"] != audio.ACE_STEP_RUNTIME_ADAPTER:
        raise WorkerIdentityError("worker_profile.runtime_adapter is unsupported")
    if value["runtime_digest"] != audio.ACE_STEP_RUNTIME_DIGEST:
        raise WorkerIdentityError("worker_profile.runtime_digest is unsupported")
    if value["recipe_root"] != audio.ACE_STEP_RECIPE_ROOT:
        raise WorkerIdentityError("worker_profile.recipe_root is unsupported")
    if value["capability_tier"] not in audio.ACE_STEP_CAPABILITY_TIERS:
        raise WorkerIdentityError("worker_profile.capability_tier is unsupported")
    elapsed = value["canary_elapsed_seconds"]
    if elapsed is not None and (not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or elapsed < 0 or elapsed > 86400):
        raise WorkerIdentityError("worker_profile.canary_elapsed_seconds is invalid")
    return dict(value)


async def verify_registration(
    *,
    proof: Any,
    payout_wallet: str,
    worker_name: str,
    models: Sequence[str],
    job_types: Sequence[str],
    bridge_agent: str,
    worker_profile: Mapping[str, Any] | None,
    required: bool,
    now: int | None = None,
) -> VerifiedWorkerIdentity | None:
    if proof is None:
        if required:
            raise WorkerIdentityError("payout-wallet worker delegation is required")
        return None
    verified = _verify_registration_proof(
        proof=proof,
        payout_wallet=payout_wallet,
        worker_name=worker_name,
        models=models,
        job_types=job_types,
        bridge_agent=bridge_agent,
        worker_profile=worker_profile,
        now=now,
    )
    payload = proof["payload"]
    settings = get_settings()
    ttl = max(settings.worker_registration_skew_seconds * 2, 60)
    nonce_key = hashlib.sha256(
        f"{verified.signer_address}:{payload['nonce']}".encode(),
    ).hexdigest()
    try:
        fresh = await get_redis().set(f"{NONCE_PREFIX}{nonce_key}", "1", nx=True, ex=ttl)
    except Exception as exc:
        raise WorkerIdentityError("worker registration freshness store unavailable") from exc
    if not fresh:
        raise WorkerIdentityError("worker registration proof was already used")
    return verified


def _verify_registration_proof(
    *,
    proof: Any,
    payout_wallet: str,
    worker_name: str,
    models: Sequence[str],
    job_types: Sequence[str],
    bridge_agent: str,
    worker_profile: Mapping[str, Any] | None,
    now: int | None,
) -> VerifiedWorkerIdentity:
    if not payout_wallet or not is_address(payout_wallet):
        raise WorkerIdentityError("authenticated account has no valid payout wallet")
    if not isinstance(proof, Mapping) or set(proof) != {"payload", "signature", "delegation"}:
        raise WorkerIdentityError("worker identity proof is malformed")
    payload = proof["payload"]
    certificate = proof["delegation"]
    if not isinstance(payload, Mapping) or set(payload) != REGISTRATION_FIELDS:
        raise WorkerIdentityError("worker registration payload is malformed")
    if not isinstance(certificate, Mapping) or set(certificate) != {"payload", "signature"}:
        raise WorkerIdentityError("worker delegation certificate is malformed")
    delegation = certificate["payload"]
    if not isinstance(delegation, Mapping) or set(delegation) != DELEGATION_FIELDS:
        raise WorkerIdentityError("worker delegation payload is malformed")

    settings = get_settings()
    current = int(now if now is not None else time.time())
    _validate_public_registration_fields(worker_name, models, job_types, bridge_agent)
    _validate_delegation(delegation, settings, current)
    signer = _address(delegation["worker_signer"], "worker signer")
    expected_profile_digest = worker_profile["digest"] if worker_profile else None
    if expected_profile_digest:
        approved = {item.strip().lower() for item in settings.approved_worker_profile_digests.split(",") if item.strip()}
        if expected_profile_digest.lower() not in approved:
            raise WorkerIdentityError("worker profile digest is not approved by Core")
    expected = {
        "version": IDENTITY_VERSION,
        "timestamp": payload["timestamp"],
        "nonce": payload["nonce"],
        "worker_signer": signer,
        "worker_name": worker_name,
        "models": list(models),
        "job_types": list(job_types),
        "bridge_agent": bridge_agent,
        "profile_digest": expected_profile_digest,
        "profile_recipe_root": worker_profile["recipe_root"] if worker_profile else None,
    }
    if dict(payload) != expected:
        raise WorkerIdentityError("worker registration proof does not match advertised capabilities")
    timestamp = _integer(payload["timestamp"], "registration timestamp")
    if abs(current - timestamp) > settings.worker_registration_skew_seconds:
        raise WorkerIdentityError("worker registration proof is stale")
    if not _is_hex(payload["nonce"], 32):
        raise WorkerIdentityError("worker registration nonce must be 16 random bytes")
    if delegation["payout_wallet"] != payout_wallet.lower():
        raise WorkerIdentityError("delegation payout wallet does not match the authenticated account")
    if delegation["worker_name"] != worker_name:
        raise WorkerIdentityError("delegation targets a different worker name")
    if delegation["worker_signer"] != signer:
        raise WorkerIdentityError("delegation worker signer is not canonical")

    recovered_wallet = _recover(delegation_message(delegation), certificate["signature"], "delegation")
    if recovered_wallet != payout_wallet.lower():
        raise WorkerIdentityError("delegation was not signed by the payout wallet")
    recovered_worker = _recover(registration_message(payload), proof["signature"], "registration")
    if recovered_worker != signer:
        raise WorkerIdentityError("registration was not signed by the delegated worker key")
    return VerifiedWorkerIdentity(
        signer_address=signer,
        payout_wallet=payout_wallet.lower(),
        delegation_id=delegation["delegation_id"],
        expires_at=delegation["expires_at"],
    )


def _validate_delegation(payload: Mapping[str, Any], settings, now: int) -> None:
    if payload.get("version") != IDENTITY_VERSION:
        raise WorkerIdentityError("unsupported worker delegation version")
    if _integer(payload.get("chain_id"), "delegation chain ID") != settings.worker_identity_chain_id:
        raise WorkerIdentityError("worker delegation targets the wrong chain")
    if payload.get("audience") != settings.worker_identity_audience:
        raise WorkerIdentityError("worker delegation targets the wrong Core audience")
    _address(payload.get("payout_wallet"), "payout wallet")
    _address(payload.get("worker_signer"), "worker signer")
    if not _is_hex(payload.get("delegation_id"), 32):
        raise WorkerIdentityError("worker delegation ID is invalid")
    if not isinstance(payload.get("worker_name"), str) or not payload["worker_name"]:
        raise WorkerIdentityError("worker delegation name is invalid")
    issued_at = _integer(payload.get("issued_at"), "delegation issued_at")
    expires_at = _integer(payload.get("expires_at"), "delegation expires_at")
    if expires_at <= issued_at or expires_at - issued_at > MAX_DELEGATION_SECONDS:
        raise WorkerIdentityError("worker delegation lifetime is invalid")
    if issued_at > now + settings.worker_registration_skew_seconds:
        raise WorkerIdentityError("worker delegation was issued in the future")
    if expires_at <= now:
        raise WorkerIdentityError("worker delegation has expired")


def _validate_public_registration_fields(
    worker_name: str,
    models: Sequence[str],
    job_types: Sequence[str],
    bridge_agent: str,
) -> None:
    if not isinstance(worker_name, str) or not worker_name or len(worker_name) > 128:
        raise WorkerIdentityError("worker name is invalid")
    if not isinstance(models, (list, tuple)) or not 1 <= len(models) <= 64:
        raise WorkerIdentityError("worker model list is invalid")
    if not all(isinstance(item, str) and 0 < len(item) <= 200 for item in models):
        raise WorkerIdentityError("worker model name is invalid")
    if not isinstance(job_types, (list, tuple)) or not 1 <= len(job_types) <= 5:
        raise WorkerIdentityError("worker job type list is invalid")
    if not all(isinstance(item, str) and 0 < len(item) <= 20 for item in job_types):
        raise WorkerIdentityError("worker job type is invalid")
    if not isinstance(bridge_agent, str) or not bridge_agent or len(bridge_agent) > 128:
        raise WorkerIdentityError("worker bridge agent is invalid")


def _recover(message: str, signature: Any, label: str) -> str:
    if not isinstance(signature, str) or len(signature) > 132:
        raise WorkerIdentityError(f"worker {label} signature is invalid")
    try:
        return Account.recover_message(encode_defunct(text=message), signature=signature).lower()
    except Exception as exc:
        raise WorkerIdentityError(f"worker {label} signature is invalid") from exc


def _address(value: Any, label: str) -> str:
    if not isinstance(value, str) or not is_address(value):
        raise WorkerIdentityError(f"{label} is not a valid EVM address")
    return value.lower()


def _integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise WorkerIdentityError(f"{label} must be an integer")
    return value


def _is_hex(value: Any, length: int) -> bool:
    return bool(isinstance(value, str) and len(value) == length and all(char in "0123456789abcdef" for char in value))
