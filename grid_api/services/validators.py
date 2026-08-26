# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Validator assignment, attestation, and scorecard storage.

This module is the boundary between preview validator evidence and
assignment-bound evidence. It deliberately does not route production traffic,
reward validators, slash workers, or move user credits. Optional paid text
audits reserve a bounded network budget here; the worker transport owns the
atomic payout-ledger terminal.
"""

from __future__ import annotations

import asyncio
import ast
import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..database import new_session
from ..safe_logging import error_type, opaque_id
from ..v2.schema import validator_assignments as assignments_t
from ..v2.schema import validator_attestations as attestations_t
from ..v2.schema import validator_probe_groups as probe_groups_t
from ..v2.schema import validators as validators_t
from ..v2.schema import workers as workers_t

logger = logging.getLogger("grid_api.validators")

VALID_VERDICTS = {"healthy", "slow", "failed"}
VERDICT_SCORE = {"healthy": 1.0, "slow": 0.75, "failed": 0.0}
VALID_AUTHORITY = {"all", "preview", "authoritative"}
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_PROBE_RESULT_BYTES = 512 * 1024
ASSIGNMENT_TTL_SECONDS = int(os.getenv("VALIDATOR_ASSIGNMENT_TTL_SECONDS", "900") or 900)
ATTESTATION_GRACE_SECONDS = max(
    60,
    int(os.getenv("VALIDATOR_ATTESTATION_GRACE_SECONDS", "1800") or 1800),
)
PROBE_TIMEOUT_SECONDS = int(os.getenv("VALIDATOR_PROBE_TIMEOUT_SECONDS", "180") or 180)
PROBE_LATENCY_BUDGET_SECONDS = int(os.getenv("VALIDATOR_PROBE_LATENCY_BUDGET_SECONDS", "30") or 30)
PROBE_MAX_ATTEMPTS = max(1, int(os.getenv("VALIDATOR_PROBE_MAX_ATTEMPTS", "2") or 2))
PROBE_LEASE_SECONDS = max(
    (PROBE_TIMEOUT_SECONDS * 2) + 60,
    int(os.getenv("VALIDATOR_PROBE_LEASE_SECONDS", "240") or 240),
)
QUORUM_MIN = max(3, int(os.getenv("VALIDATOR_QUORUM_MIN", "3") or 3))
QUORUM_TARGET = max(
    5,
    QUORUM_MIN,
    int(os.getenv("VALIDATOR_QUORUM_TARGET", "5") or 5),
)
# Canary answers are short, but reasoning models (gpt-oss/qwen3/Gemma) spend
# tokens "thinking" before the final answer. A tight cap (was 32) gets fully
# consumed by the reasoning phase → empty answer → probe returns no text →
# the model can't be scored. Give enough room to think AND answer. Overridable.
PROBE_MAX_TOKENS = max(32, int(os.getenv("VALIDATOR_PROBE_MAX_TOKENS", "512") or 512))
REGISTRATION_MAX_CLOCK_SKEW_SECONDS = max(
    60,
    int(os.getenv("VALIDATOR_REGISTRATION_MAX_CLOCK_SKEW_SECONDS", "300") or 300),
)
VALIDATOR_HEARTBEAT_FRESH_SECONDS = max(
    60,
    int(os.getenv("VALIDATOR_HEARTBEAT_FRESH_SECONDS", "900") or 900),
)
VALIDATOR_OPERATOR_SAMPLE_INTERVAL_SECONDS = max(
    60,
    int(os.getenv("VALIDATOR_OPERATOR_SAMPLE_INTERVAL_SECONDS", "300") or 300),
)

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SIG_RE = re.compile(r"^(0x)?[0-9a-fA-F]{130}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AttestationError(ValueError):
    """Raised when a submitted attestation is malformed or unverifiable."""


class AssignmentError(ValueError):
    """Raised when an assignment/probe request is invalid."""


class RegistrationError(ValueError):
    """Raised when a validator registration is malformed or unauthorized."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _hash_obj(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj).encode()).hexdigest()


def _hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


def _attestation_hash(payload: dict[str, Any], signature: str | None) -> str:
    body = _canonical({"payload": payload, "signature": signature or None})
    return hashlib.sha256(body.encode()).hexdigest()


def _payload_size(payload: dict[str, Any]) -> int:
    return len(_canonical(payload).encode())


def _bounded_probe_result(result: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe synthetic probe result within the replay limit."""
    if not isinstance(result, dict) or result.get("status") != "completed":
        raise AssignmentError("completed probe result is invalid")
    try:
        encoded = json.dumps(
            result,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AssignmentError("completed probe result is not JSON serializable") from exc
    if len(encoded) > MAX_PROBE_RESULT_BYTES:
        raise AssignmentError("completed probe result exceeds the replay limit")
    return json.loads(encoded)


def _string(payload: dict[str, Any], key: str, max_len: int) -> str | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if len(value) > max_len:
        raise AttestationError(f"payload.{key} is too long")
    return value


def _int(payload: dict[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    try:
        ivalue = int(value)
    except (TypeError, ValueError) as exc:
        raise AttestationError(f"payload.{key} must be an integer") from exc
    if ivalue < 0:
        raise AttestationError(f"payload.{key} must be non-negative")
    return ivalue


def _float(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise AttestationError(f"payload.{key} must be a number") from exc


def _normalize_signature(signature: str | None) -> str | None:
    if signature is None or signature == "":
        return None
    if not isinstance(signature, str):
        raise AttestationError("signature must be a hex string")
    sig = signature.strip()
    if not _SIG_RE.match(sig):
        raise AttestationError("signature must be a 65-byte hex signature")
    return sig if sig.startswith("0x") else f"0x{sig}"


def _validator_wallet(payload: dict[str, Any]) -> str | None:
    wallet = _string(payload, "validator", 42) or _string(payload, "validator_wallet", 42)
    if not wallet:
        return None
    if not _ADDR_RE.match(wallet):
        raise AttestationError("payload.validator must be a 20-byte 0x hex address")
    return wallet.lower()


def _signature_status(payload: dict[str, Any], signature: str | None) -> str:
    if not signature:
        return "unsigned"

    wallet = _validator_wallet(payload)
    if not wallet:
        raise AttestationError("signature requires payload.validator")

    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except ImportError:  # pragma: no cover - dependency exists in production requirements
        return "unverified:dependency"

    try:
        recovered = Account.recover_message(
            encode_defunct(text=_canonical(payload)),
            signature=signature,
        )
    except Exception as exc:
        raise AttestationError("signature verification failed") from exc
    if recovered.lower() != wallet.lower():
        raise AttestationError("signature does not match validator wallet")
    return "verified"


def _registration_capabilities(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("capabilities")
    if not isinstance(raw, list) or not raw:
        raise RegistrationError("payload.capabilities must be a non-empty list")
    capabilities: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > 128:
            raise RegistrationError("payload.capabilities contains an invalid capability")
        capabilities.append(item.strip())
    if len(capabilities) > 64:
        raise RegistrationError("payload.capabilities may contain at most 64 entries")
    return sorted(set(capabilities))


async def register_validator(
    *,
    account_id,
    account_wallet: str | None,
    payload: dict[str, Any],
    signature: str | None,
) -> dict[str, Any]:
    """Create or refresh one wallet-proven validator registration."""
    if not isinstance(payload, dict) or _payload_size(payload) > MAX_PAYLOAD_BYTES:
        raise RegistrationError("registration payload must be a bounded object")
    if payload.get("registration_schema") != "aipg.validator.registration.v1":
        raise RegistrationError("unsupported validator registration schema")
    try:
        wallet = _validator_wallet(payload)
    except AttestationError as exc:
        raise RegistrationError(str(exc)) from exc
    linked_wallet = (account_wallet or "").strip().lower()
    if not linked_wallet or not _ADDR_RE.match(linked_wallet):
        raise RegistrationError("validator account must have a linked wallet")
    if wallet != linked_wallet:
        raise RegistrationError("validator signing wallet must match the account's linked wallet")
    normalized_signature = _normalize_signature(signature)
    if not normalized_signature:
        raise RegistrationError("validator registration requires a wallet signature")
    try:
        signature_status = _signature_status(payload, normalized_signature)
    except AttestationError as exc:
        raise RegistrationError(str(exc)) from exc
    if signature_status != "verified":
        raise RegistrationError("validator registration signature could not be verified")
    software_version = _string(payload, "software_version", 64)
    if not software_version:
        raise RegistrationError("payload.software_version is required")
    signed_ts = _int(payload, "ts")
    if signed_ts is None:
        raise RegistrationError("payload.ts is required")
    now = _now()
    if abs(int(now.timestamp()) - signed_ts) > REGISTRATION_MAX_CLOCK_SKEW_SECONDS:
        raise RegistrationError("validator registration timestamp is outside the allowed window")
    capabilities = _registration_capabilities(payload)

    async with await new_session() as session:
        existing = (
            await session.execute(
                sa.select(validators_t).where(validators_t.c.signing_wallet == wallet),
            )
        ).mappings().first()
        existing_account = (
            await session.execute(
                sa.select(validators_t.c.id, validators_t.c.signing_wallet).where(
                    validators_t.c.account_id == account_id
                )
            )
        ).mappings().first()
        if existing and existing["account_id"] != account_id:
            raise RegistrationError("validator signing wallet belongs to another account")
        if existing_account and existing_account["signing_wallet"] != wallet:
            raise RegistrationError("validator account already has a different signing wallet")
        if existing and existing["status"] == "revoked":
            raise RegistrationError("validator registration is revoked")
        if existing:
            validator_id = existing["id"]
            await session.execute(
                sa.update(validators_t)
                .where(validators_t.c.id == validator_id)
                .values(
                    software_version=software_version,
                    capabilities=capabilities,
                    registration_signature=normalized_signature,
                    status="active",
                    last_heartbeat=now,
                    updated=now,
                ),
            )
            created = False
        else:
            validator_id = f"val_{uuid4().hex}"
            await session.execute(
                sa.insert(validators_t).values(
                    id=validator_id,
                    account_id=account_id,
                    signing_wallet=wallet,
                    software_version=software_version,
                    capabilities=capabilities,
                    registration_signature=normalized_signature,
                    status="active",
                    last_heartbeat=now,
                    created=now,
                    updated=now,
                ),
            )
            created = True
        await session.commit()
    return {
        "validator_id": validator_id,
        "signing_wallet": wallet,
        "software_version": software_version,
        "capabilities": capabilities,
        "status": "active",
        "created": created,
        "last_heartbeat": now.isoformat(),
        "economic_effect": "none",
    }


async def validator_for_account(
    *,
    account_id,
    statuses: tuple[str, ...] = ("active", "suspended"),
) -> dict[str, Any]:
    """Return this account's registration without trusting its current wallet link."""
    async with await new_session() as session:
        row = (
            await session.execute(
                sa.select(validators_t).where(
                    validators_t.c.account_id == account_id,
                    validators_t.c.status.in_(statuses),
                ),
            )
        ).mappings().first()
    if not row:
        raise RegistrationError("validator registration required")
    return dict(row)


def _fresh_control_payload(
    payload: dict[str, Any],
    signature: str | None,
    *,
    schema_key: str,
    schema_value: str,
) -> tuple[str, str]:
    if not isinstance(payload, dict) or _payload_size(payload) > MAX_PAYLOAD_BYTES:
        raise RegistrationError("validator control payload must be a bounded object")
    if payload.get(schema_key) != schema_value:
        raise RegistrationError(f"unsupported validator {schema_key.removesuffix('_schema')} schema")
    try:
        wallet = _validator_wallet(payload)
    except AttestationError as exc:
        raise RegistrationError(str(exc)) from exc
    normalized_signature = _normalize_signature(signature)
    if not normalized_signature:
        raise RegistrationError("validator control requires a wallet signature")
    try:
        signature_status = _signature_status(payload, normalized_signature)
    except AttestationError as exc:
        raise RegistrationError(str(exc)) from exc
    if signature_status != "verified":
        raise RegistrationError("validator control signature could not be verified")
    signed_ts = _int(payload, "ts")
    if signed_ts is None:
        raise RegistrationError("payload.ts is required")
    if abs(int(_now().timestamp()) - signed_ts) > REGISTRATION_MAX_CLOCK_SKEW_SECONDS:
        raise RegistrationError("validator control timestamp is outside the allowed window")
    return wallet, normalized_signature


async def suspend_validator(
    *,
    account_id,
    payload: dict[str, Any],
    signature: str | None,
) -> dict[str, Any]:
    """Idempotently stop new work using a fresh current-wallet signature."""
    wallet, _ = _fresh_control_payload(
        payload,
        signature,
        schema_key="suspension_schema",
        schema_value="aipg.validator.suspension.v1",
    )
    validator_id = _string(payload, "validator_id", 96)
    if not validator_id:
        raise RegistrationError("payload.validator_id is required")
    now = _now()
    async with await new_session() as session:
        row = (
            await session.execute(
                sa.select(validators_t)
                .where(validators_t.c.account_id == account_id)
                .with_for_update()
            )
        ).mappings().first()
        if not row or row["id"] != validator_id:
            raise RegistrationError("validator registration does not match this account")
        if row["status"] == "revoked":
            raise RegistrationError("validator registration is revoked")
        if row["signing_wallet"] != wallet:
            raise RegistrationError("suspension must be signed by the registered wallet")
        if row["status"] != "suspended":
            await session.execute(
                sa.update(validators_t)
                .where(validators_t.c.id == validator_id)
                .values(status="suspended", updated=now)
            )
        await session.commit()
    return {
        "validator_id": validator_id,
        "status": "suspended",
        "economic_effect": "none",
    }


async def rotate_validator(
    *,
    account_id,
    account_wallet: str | None,
    payload: dict[str, Any],
    signature: str | None,
) -> dict[str, Any]:
    """Replace a validator signing wallet after the account links the new wallet."""
    wallet, normalized_signature = _fresh_control_payload(
        payload,
        signature,
        schema_key="rotation_schema",
        schema_value="aipg.validator.rotation.v1",
    )
    linked_wallet = (account_wallet or "").strip().lower()
    if not linked_wallet or not _ADDR_RE.match(linked_wallet):
        raise RegistrationError("validator account must have a linked replacement wallet")
    if wallet != linked_wallet:
        raise RegistrationError("replacement signing wallet must match the account's linked wallet")
    validator_id = _string(payload, "validator_id", 96)
    previous_wallet = _string(payload, "previous_signing_wallet", 42)
    if not validator_id or not previous_wallet or not _ADDR_RE.match(previous_wallet):
        raise RegistrationError("rotation requires validator_id and previous_signing_wallet")
    previous_wallet = previous_wallet.lower()
    if wallet == previous_wallet:
        raise RegistrationError("replacement signing wallet must differ from the previous wallet")
    software_version = _string(payload, "software_version", 64)
    if not software_version:
        raise RegistrationError("payload.software_version is required")
    capabilities = _registration_capabilities(payload)
    now = _now()
    async with await new_session() as session:
        row = (
            await session.execute(
                sa.select(validators_t)
                .where(validators_t.c.account_id == account_id)
                .with_for_update()
            )
        ).mappings().first()
        if not row or row["id"] != validator_id:
            raise RegistrationError("validator registration does not match this account")
        if row["status"] == "revoked":
            raise RegistrationError("validator registration is revoked")
        if row["signing_wallet"] == wallet and row["registration_signature"] == normalized_signature:
            return {
                "validator_id": validator_id,
                "signing_wallet": wallet,
                "status": row["status"],
                "rotated": False,
                "economic_effect": "none",
            }
        if row["signing_wallet"] != previous_wallet:
            raise RegistrationError("previous signing wallet does not match the registration")
        wallet_owner = await session.scalar(
            sa.select(validators_t.c.id).where(validators_t.c.signing_wallet == wallet)
        )
        if wallet_owner and wallet_owner != validator_id:
            raise RegistrationError("replacement signing wallet belongs to another validator")
        await session.execute(
            sa.update(validators_t)
            .where(validators_t.c.id == validator_id)
            .values(
                signing_wallet=wallet,
                software_version=software_version,
                capabilities=capabilities,
                registration_signature=normalized_signature,
                status="active",
                last_heartbeat=now,
                updated=now,
            )
        )
        await session.commit()
    return {
        "validator_id": validator_id,
        "signing_wallet": wallet,
        "status": "active",
        "rotated": True,
        "last_heartbeat": now.isoformat(),
        "economic_effect": "none",
    }


async def active_validator(*, account_id, signing_wallet: str | None) -> dict[str, Any]:
    wallet = (signing_wallet or "").strip().lower()
    if not wallet:
        raise RegistrationError("validator account must have a linked wallet")
    async with await new_session() as session:
        row = (
            await session.execute(
                sa.select(validators_t).where(
                    validators_t.c.account_id == account_id,
                    validators_t.c.signing_wallet == wallet,
                    validators_t.c.status == "active",
                ),
            )
        ).mappings().first()
    if not row:
        raise RegistrationError("active validator registration required")
    return dict(row)


async def heartbeat_validator(
    *,
    account_id,
    signing_wallet: str | None,
    software_version: str,
    capabilities: list[str],
) -> dict[str, Any]:
    validator = await active_validator(account_id=account_id, signing_wallet=signing_wallet)
    if not software_version or len(software_version) > 64:
        raise RegistrationError("software_version is invalid")
    normalized_capabilities = _registration_capabilities({"capabilities": capabilities})
    now = _now()
    sample_cutoff = now - timedelta(seconds=VALIDATOR_OPERATOR_SAMPLE_INTERVAL_SECONDS)
    sample_due = sa.and_(
        validators_t.c.independence_status.in_(("candidate", "verified")),
        sa.or_(
            validators_t.c.last_heartbeat_sampled_at.is_(None),
            validators_t.c.last_heartbeat_sampled_at <= sample_cutoff,
        ),
    )
    async with await new_session() as session:
        await session.execute(
            sa.update(validators_t)
            .where(validators_t.c.id == validator["id"], validators_t.c.status == "active")
            .values(
                software_version=software_version,
                capabilities=normalized_capabilities,
                last_heartbeat=now,
                heartbeat_sample_count=sa.case(
                    (sample_due, validators_t.c.heartbeat_sample_count + 1),
                    else_=validators_t.c.heartbeat_sample_count,
                ),
                last_heartbeat_sampled_at=sa.case(
                    (sample_due, now),
                    else_=validators_t.c.last_heartbeat_sampled_at,
                ),
                updated=now,
            ),
        )
        await session.commit()
    return {
        "validator_id": validator["id"],
        "status": "active",
        "last_heartbeat": now.isoformat(),
        "economic_effect": "none",
    }


async def _operator_already_assigned(
    session,
    *,
    probe_group_id: str,
    validator_id: str,
    operator_group_id: str | None,
) -> bool:
    """Return true when this registration or its reviewed control group has a seat."""
    conditions = [assignments_t.c.validator_id == validator_id]
    if operator_group_id:
        conditions.append(validators_t.c.operator_group_id == operator_group_id)
    count = await session.scalar(
        sa.select(sa.func.count())
        .select_from(
            assignments_t.outerjoin(
                validators_t,
                validators_t.c.id == assignments_t.c.validator_id,
            )
        )
        .where(
            assignments_t.c.probe_group_id == probe_group_id,
            sa.or_(*conditions),
        )
    )
    return bool(count)


_TEXT_CHALLENGE_KINDS = (
    "echo",
    "math",
    "json.object",
    "context.retrieve",
    "context.retrieve.16k",
    "context.retrieve.32k",
    "logic.steps",
    "code.function",
    "tool.call",
    "tool.chain",
    "stop.sequence",
    "token.limit",
)
_TEXT_CHALLENGE_CAPABILITIES = {
    "echo": "text.instruction.v1",
    "math": "text.reasoning.v1",
    "json.object": "text.structured.v1",
    "context.retrieve": "text.context.4k.v1",
    "context.retrieve.16k": "text.context.16k.v1",
    "context.retrieve.32k": "text.context.32k.v1",
    "logic.steps": "text.reasoning.multistep.v1",
    "code.function": "text.code.v1",
    "tool.call": "text.tool_call.v1",
    "tool.chain": "text.tool_chain.v1",
    "stop.sequence": "text.stop_sequence.v1",
    "token.limit": "text.token_limit.v1",
}

_TEXT_PROTOCOL_CAPABILITIES = frozenset(
    {
        "text.instruction.v1",
        "text.structured.v1",
        "text.stop_sequence.v1",
        "text.token_limit.v1",
        "text.tool_call.v1",
    },
)
_TEXT_BATCH_SCORING_POLICY = "text.generated.v8"


def _score_dimension(modality: str | None, capability: str | None) -> str:
    """Classify evidence without implying that a canary is a quality score."""
    normalized_modality = str(modality or "")
    normalized_capability = str(capability or "")
    if normalized_capability in _TEXT_PROTOCOL_CAPABILITIES:
        return "protocol_conformance"
    if normalized_capability.startswith("text."):
        return "capability"
    if "fidelity" in normalized_capability:
        return "fidelity"
    if normalized_modality in {"image", "video", "audio"}:
        return "protocol_conformance"
    return "availability"


def _quality_eligible(modality: str | None, capability: str | None) -> bool:
    """Current generated probes are not blind workload quality evidence."""
    return _score_dimension(modality, capability) == "quality"

# Leave headroom for tokenizer differences and request framing. The challenge
# prompts are counted with the Grid's model-agnostic tokenizer, while a worker's
# advertised context limit belongs to its backend tokenizer.
_TEXT_CAPABILITY_MIN_WORKER_CONTEXT = {
    "text.context.4k.v1": 8_192,
    "text.context.16k.v1": 32_768,
    "text.context.32k.v1": 65_536,
}


def _supported_text_challenges(capabilities: list[str] | None) -> tuple[tuple[str, ...], set[str]]:
    registered = {str(value) for value in (capabilities or [])}
    supported_capabilities = set(registered)
    # Compatibility for pre-v0.1 nodes. They implemented only exact echo and
    # generated arithmetic under the coarse text.basic.v1 label.
    if "text.basic.v1" in registered:
        supported_capabilities.update({"text.instruction.v1", "text.reasoning.v1"})
    if {"text.instruction.v1", "text.reasoning.v1"}.issubset(registered):
        supported_capabilities.add("text.basic.v1")
    kinds = tuple(
        kind
        for kind in _TEXT_CHALLENGE_KINDS
        if _TEXT_CHALLENGE_CAPABILITIES[kind] in supported_capabilities
    )
    return kinds, supported_capabilities


def _worker_eligible_text_challenges(
    challenge_kinds: tuple[str, ...],
    worker: dict[str, Any],
) -> tuple[str, ...]:
    try:
        max_context = int(worker.get("max_context_length") or 0)
    except (TypeError, ValueError):
        max_context = 0
    return tuple(
        kind
        for kind in challenge_kinds
        if max_context
        >= _TEXT_CAPABILITY_MIN_WORKER_CONTEXT.get(
            _TEXT_CHALLENGE_CAPABILITIES[kind],
            0,
        )
    )


def _worker_supports_text_capability(capability: str, worker: dict[str, Any]) -> bool:
    try:
        max_context = int(worker.get("max_context_length") or 0)
    except (TypeError, ValueError):
        max_context = 0
    return max_context >= _TEXT_CAPABILITY_MIN_WORKER_CONTEXT.get(capability, 0)


async def _text_group_cooldown_active(
    session,
    *,
    worker_id: str,
    model: str,
    now: datetime,
) -> bool:
    """Keep preview validation from becoming an unlimited free-work route."""
    interval = max(
        300,
        int(get_settings().validator_text_group_min_interval_seconds),
    )
    latest = await session.scalar(
        sa.select(sa.func.max(probe_groups_t.c.created)).where(
            probe_groups_t.c.target_worker_id == worker_id,
            probe_groups_t.c.model == model,
            probe_groups_t.c.modality == "text",
        )
    )
    return bool(latest and _aware(latest) >= now - timedelta(seconds=interval))


def media_validation_policy() -> dict[str, Any]:
    """Return the fail-closed assignment gate without exposing private state."""
    settings = get_settings()
    contract = settings.validator_media_bond_contract.strip().lower()
    verifier = settings.validator_media_bond_verifier_version.strip()
    reasons: list[str] = []
    if not settings.validator_media_probe_enabled:
        reasons.append("operator gate disabled")
    if not _ADDR_RE.fullmatch(contract):
        reasons.append("reviewed bond contract not configured")
    if not verifier:
        reasons.append("bond verifier version not configured")
    if settings.validator_media_bond_chain_id <= 0:
        reasons.append("bond chain id is invalid")
    if settings.validator_media_minimum_bond_raw <= 0:
        reasons.append("minimum bond is not configured")
    if not 0 <= settings.validator_media_minimum_quality_pass_rate <= 1:
        reasons.append("quality threshold is invalid")
    if settings.validator_media_max_output_bytes <= 0:
        reasons.append("media byte limit is invalid")
    if settings.validator_media_probe_timeout_seconds <= 0:
        reasons.append("media probe timeout is invalid")
    return {
        "enabled": not reasons,
        "modality": "image",
        "capability": "image.fidelity.v1",
        "economic_effect": "none",
        "reasons": reasons,
        "chain_id": settings.validator_media_bond_chain_id,
        "bond_contract": contract,
        "bond_verifier_version": verifier,
        "minimum_bond_raw": settings.validator_media_minimum_bond_raw,
        "minimum_quality_pass_rate": settings.validator_media_minimum_quality_pass_rate,
        "max_output_bytes": settings.validator_media_max_output_bytes,
        "probe_timeout_seconds": settings.validator_media_probe_timeout_seconds,
    }


def video_validation_policy() -> dict[str, Any]:
    """Return the independent, fail-closed video-contract assignment gate."""
    settings = get_settings()
    reasons: list[str] = []
    if not settings.validator_media_probe_enabled:
        reasons.append("media probe master gate disabled")
    if not settings.validator_video_probe_enabled:
        reasons.append("video probe operator gate disabled")
    if settings.validator_media_max_output_bytes <= 0:
        reasons.append("media byte limit is invalid")
    if settings.validator_media_probe_timeout_seconds <= 0:
        reasons.append("media probe timeout is invalid")
    return {
        "enabled": not reasons,
        "modality": "video",
        "capability": "video.contract.v1",
        "economic_effect": "none",
        "reasons": reasons,
        "max_output_bytes": settings.validator_media_max_output_bytes,
        "probe_timeout_seconds": settings.validator_media_probe_timeout_seconds,
    }


_MEDIA_OBJECTS = (
    "ceramic teapot", "brass telescope", "origami crane", "glass lighthouse",
    "wooden airship", "silver hourglass", "clockwork violin", "stone bicycle",
)
_MEDIA_SETTINGS = (
    "inside an overgrown library", "beside a frozen lake", "under a glass dome",
    "on a stormy coast", "above a field of clouds", "in a moonlit workshop",
)
_MEDIA_LIGHTING = (
    "soft morning light", "hard rim lighting", "warm lantern light",
    "diffuse overcast light", "high-contrast studio lighting",
)
_MEDIA_COMPOSITIONS = (
    "symmetrical composition", "overhead composition", "wide establishing view",
    "low-angle close view", "layered foreground and background",
)


def _make_image_prompt() -> str:
    token = secrets.token_hex(4)
    return (
        f"A {_MEDIA_OBJECTS[secrets.randbelow(len(_MEDIA_OBJECTS))]} "
        f"{_MEDIA_SETTINGS[secrets.randbelow(len(_MEDIA_SETTINGS))]}, "
        f"{_MEDIA_COMPOSITIONS[secrets.randbelow(len(_MEDIA_COMPOSITIONS))]}, "
        f"{_MEDIA_LIGHTING[secrets.randbelow(len(_MEDIA_LIGHTING))]}, "
        f"small engraved mark {token}, highly detailed"
    )


def _make_video_prompt() -> str:
    token = secrets.token_hex(4)
    subject = _MEDIA_OBJECTS[secrets.randbelow(len(_MEDIA_OBJECTS))]
    setting = _MEDIA_SETTINGS[secrets.randbelow(len(_MEDIA_SETTINGS))]
    lighting = _MEDIA_LIGHTING[secrets.randbelow(len(_MEDIA_LIGHTING))]
    action = (
        "rotating slowly while the camera tracks left",
        "moving forward while the camera pulls back",
        "swaying in the wind during a slow orbiting shot",
        "crossing the frame during a steady rightward pan",
    )
    return (
        f"A {subject} {setting}, {action[secrets.randbelow(len(action))]}, "
        f"{lighting}, continuous motion, small engraved mark {token}, no cuts"
    )


def _probe_number(recipe, name: str, preferred: float) -> int | float:
    bounds = recipe.clamps.get(name)
    value = preferred
    if bounds and len(bounds) == 2:
        value = max(float(bounds[0]), min(float(bounds[1]), value))
    return int(value) if name in {"width", "height", "steps"} else value


def _image_recipe_for_worker(worker: dict[str, Any]):
    """Return governed fidelity recipes the connected worker can execute."""
    from . import recipes

    advertised = {str(value) for value in (worker.get("models") or [])}
    eligible = []
    for recipe in recipes.list_recipes():
        required = set(recipe.required_models or [recipe.model_name])
        if (
            recipe.job_type == "image"
            and recipe.deterministic
            and recipe.recipe_id is not None
            and _SHA256_RE.fullmatch(recipe.model_digest)
            and {"prompt", "seed", "width", "height"}.issubset(recipe.vars)
            and "image" not in recipe.vars
            and required.issubset(advertised)
        ):
            eligible.append(recipe)
    return sorted(eligible, key=lambda item: (item.model_name.lower(), item.recipe_root))


def _video_recipe_for_worker(worker: dict[str, Any]):
    """Return governed text-to-video recipes with an explicit timing contract."""
    from . import recipes

    advertised = {str(value) for value in (worker.get("models") or [])}
    eligible = []
    required_inputs = {"prompt", "seed", "width", "height", "seconds", "fps"}
    for recipe in recipes.list_recipes():
        required = set(recipe.required_models or [recipe.model_name])
        if (
            recipe.job_type == "video"
            and recipe.recipe_id is not None
            and required_inputs.issubset(recipe.vars)
            and "image" not in recipe.vars
            and required.issubset(advertised)
        ):
            eligible.append(recipe)
    return sorted(eligible, key=lambda item: (item.model_name.lower(), item.recipe_root))


def _make_image_challenge(recipe, reference_worker_ids: list[str]) -> dict[str, Any]:
    inputs: dict[str, Any] = {
        "prompt": _make_image_prompt(),
        "seed": secrets.randbits(53) or 1,
        "width": _probe_number(recipe, "width", 512),
        "height": _probe_number(recipe, "height", 512),
    }
    if "steps" in recipe.vars:
        inputs["steps"] = _probe_number(recipe, "steps", 12)
    if "cfg" in recipe.vars:
        inputs["cfg"] = _probe_number(recipe, "cfg", 1.0)
    for name in ("sampler", "scheduler"):
        if name in recipe.vars and recipe.enums.get(name):
            inputs[name] = str(recipe.enums[name][0])
    parameters = {
        "width": inputs["width"],
        "height": inputs["height"],
        **({"steps": inputs["steps"]} if "steps" in inputs else {}),
        **({"cfg_scale": inputs["cfg"]} if "cfg" in inputs else {}),
    }
    for name in ("sampler", "scheduler"):
        if name in inputs:
            parameters[name] = inputs[name]
    return {
        "schema": "aipg.validator.media.challenge.v1",
        "kind": "image.fidelity",
        "modality": "image",
        "prompt": inputs["prompt"],
        "seed": inputs["seed"],
        "model": recipe.model_name,
        "model_digest": recipe.model_digest,
        "recipe_id": recipe.recipe_id,
        "recipe_root": recipe.recipe_root,
        "parameters": parameters,
        "reference_worker_ids": reference_worker_ids,
        "scoring_policy_id": "image.fidelity.v1",
    }


def _make_video_challenge(recipe) -> dict[str, Any]:
    from . import media

    fps = int(_probe_number(recipe, "fps", 8))
    seconds = float(_probe_number(recipe, "seconds", 2))
    width = int(_probe_number(recipe, "width", 512))
    height = int(_probe_number(recipe, "height", 512))
    try:
        frame_count, effective_seconds = media.normalize_video_timing(seconds, fps)
    except Exception as exc:
        raise AssignmentError("video recipe has no safe validator timing contract") from exc
    seed = secrets.randbits(53) or 1
    parameters: dict[str, Any] = {
        "seed": seed,
        "width": width,
        "height": height,
        "frame_count": frame_count,
        "fps": fps,
        "duration_s": effective_seconds,
        "motion_required": True,
    }
    if "steps" in recipe.vars:
        parameters["steps"] = _probe_number(recipe, "steps", 8)
    if "cfg" in recipe.vars:
        parameters["cfg_scale"] = _probe_number(recipe, "cfg", 1.0)
    for name in ("sampler", "scheduler"):
        if name in recipe.vars and recipe.enums.get(name):
            parameters[name] = str(recipe.enums[name][0])
    return {
        "schema": "aipg.validator.media.challenge.v1",
        "kind": "video.contract",
        "modality": "video",
        "prompt": _make_video_prompt(),
        "seed": seed,
        "model": recipe.model_name,
        "recipe_id": recipe.recipe_id,
        "recipe_root": recipe.recipe_root,
        "parameters": parameters,
        "reference_worker_ids": [],
        "scoring_policy_id": "video.contract.v1",
    }


def _make_text_challenge(kind: str | None = None) -> dict[str, Any]:
    """Create one private, randomized text challenge.

    The assignment carries only a one-way expected-answer commitment. The
    optional kind is for deterministic tests; production chooses with
    cryptographic randomness so worker order cannot fingerprint a family.
    """
    selected = kind or secrets.choice(_TEXT_CHALLENGE_KINDS)
    # A shared v8 batch fixes the capability and concrete canary kind while
    # issuing fresh values to every validator. Math needs explicit add/mul
    # selectors so every member stays in the same comparable lane.
    valid_selectors = {*_TEXT_CHALLENGE_KINDS, "math.add", "math.mul"}
    if selected not in valid_selectors:
        raise ValueError("unsupported text challenge kind")

    challenge_max_tokens = PROBE_MAX_TOKENS
    if selected == "echo":
        token = secrets.token_hex(8).upper()
        prompt = f"Reply with exactly this token and nothing else: {token}"
        expected = token
        kind = "echo"
        capability = "text.instruction.v1"
    elif selected in {"math", "math.add", "math.mul"}:
        a = secrets.randbelow(80) + 11
        b = secrets.randbelow(80) + 11
        add = selected == "math.add" or (
            selected == "math" and bool(secrets.randbelow(2))
        )
        if add:
            prompt = f"What is {a} + {b}? Reply with only the number."
            expected = str(a + b)
            kind = "math.add"
        else:
            prompt = f"What is {a} * {b}? Reply with only the number."
            expected = str(a * b)
            kind = "math.mul"
        capability = "text.reasoning.v1"
    elif selected == "json.object":
        key_number = f"count_{secrets.token_hex(3)}"
        key_token = f"token_{secrets.token_hex(3)}"
        key_flag = f"ready_{secrets.token_hex(3)}"
        expected_obj = {
            key_number: secrets.randbelow(9000) + 1000,
            key_token: secrets.token_hex(6).upper(),
            key_flag: bool(secrets.randbelow(2)),
        }
        expected = _canonical(expected_obj)
        prompt = (
            "Return exactly one valid JSON object and no markdown or explanation. "
            f"Set {key_number!r} to {expected_obj[key_number]}, "
            f"{key_token!r} to {expected_obj[key_token]!r}, and "
            f"{key_flag!r} to {str(expected_obj[key_flag]).lower()}."
        )
        kind = "json.object"
        capability = "text.structured.v1"
    elif selected in {
        "context.retrieve",
        "context.retrieve.16k",
        "context.retrieve.32k",
    }:
        record_count = {
            "context.retrieve": 100,
            "context.retrieve.16k": 400,
            "context.retrieve.32k": 800,
        }[selected]
        target_index = secrets.randbelow(record_count)
        records: list[str] = []
        target_key = ""
        expected = ""
        for index in range(record_count):
            key = secrets.token_hex(5).upper()
            value = secrets.token_hex(8).upper()
            filler = secrets.token_hex(12).upper()
            records.append(
                f"record={index:03d} key={key} value={value} checksum={filler}"
            )
            if index == target_index:
                target_key = key
                expected = value
        prompt = (
            "Read the record set below. Find the record whose key exactly equals "
            f"{target_key}. Reply with only that record's value.\n\n"
            + "\n".join(records)
        )
        kind = selected
        capability = {
            "context.retrieve": "text.context.4k.v1",
            "context.retrieve.16k": "text.context.16k.v1",
            "context.retrieve.32k": "text.context.32k.v1",
        }[selected]
    elif selected == "logic.steps":
        value = secrets.randbelow(18) + 3
        start = value
        operations: list[str] = []
        for _ in range(4):
            operand = secrets.randbelow(7) + 2
            operation = secrets.randbelow(3)
            if operation == 0:
                value += operand
                operations.append(f"add {operand}")
            elif operation == 1:
                value -= operand
                operations.append(f"subtract {operand}")
            else:
                value *= operand
                operations.append(f"multiply by {operand}")
        expected = str(value)
        prompt = (
            f"Start with {start}. In order, "
            + ", then ".join(operations)
            + ". Reply with only the final integer."
        )
        kind = "logic.steps"
        capability = "text.reasoning.multistep.v1"
    elif selected == "code.function":
        function_name = f"transform_{secrets.token_hex(4)}"
        multiplier = secrets.randbelow(8) + 2
        offset = secrets.randbelow(41) - 20
        modulus = secrets.randbelow(81) + 17
        adjustment = secrets.randbelow(9) + 1
        test_inputs: list[int] = []
        while len(test_inputs) < 7:
            value = secrets.randbelow(201) - 100
            if value not in test_inputs:
                test_inputs.append(value)
        outputs = [
            ((value * multiplier + offset) % modulus) - adjustment
            for value in test_inputs
        ]
        expected = _canonical(outputs)
        prompt = (
            f"Write one Python function named {function_name} that accepts exactly one "
            "integer argument named x. It must multiply x by "
            f"{multiplier}, add {offset}, take the result modulo {modulus} using Python "
            f"integer semantics, then subtract {adjustment}. Return only the function "
            "definition with no markdown, imports, calls, annotations, or explanation."
        )
        kind = "code.function"
        capability = "text.code.v1"
    elif selected == "tool.call":
        function_name = f"record_{secrets.token_hex(4)}"
        number_field = f"count_{secrets.token_hex(3)}"
        token_field = f"token_{secrets.token_hex(3)}"
        arguments = {
            number_field: secrets.randbelow(9000) + 1000,
            token_field: secrets.token_hex(6).upper(),
        }
        expected = _canonical({"name": function_name, "arguments": arguments})
        prompt = (
            f"Call the {function_name} tool exactly once. Set {number_field!r} to "
            f"{arguments[number_field]} and {token_field!r} to {arguments[token_field]!r}. "
            "Do not answer in text."
        )
        kind = "tool.call"
        capability = "text.tool_call.v1"
        tools = [{
            "type": "function",
            "function": {
                "name": function_name,
                "description": "Record the two exact values requested by the user.",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        number_field: {"type": "integer"},
                        token_field: {"type": "string"},
                    },
                    "required": [number_field, token_field],
                    "additionalProperties": False,
                },
            },
        }]
        tool_choice = {"type": "function", "function": {"name": function_name}}
    elif selected == "tool.chain":
        lookup_name = f"lookup_{secrets.token_hex(4)}"
        submit_name = f"submit_{secrets.token_hex(4)}"
        lookup_field = f"key_{secrets.token_hex(3)}"
        total_field = f"total_{secrets.token_hex(3)}"
        token_field = f"token_{secrets.token_hex(3)}"
        lookup_key = secrets.token_hex(6).upper()
        left = secrets.randbelow(80) + 11
        right = secrets.randbelow(80) + 11
        result_token = secrets.token_hex(6).upper()
        first_arguments = {lookup_field: lookup_key}
        tool_result = {"left": left, "right": right, "token": result_token}
        second_arguments = {total_field: left + right, token_field: result_token}
        first_expected = _canonical({"name": lookup_name, "arguments": first_arguments})
        second_expected = _canonical({"name": submit_name, "arguments": second_arguments})
        expected = _canonical([json.loads(first_expected), json.loads(second_expected)])
        prompt = (
            f"Call {lookup_name} exactly once with {lookup_field!r} set to {lookup_key!r}. "
            f"After the tool result arrives, add its left and right integers, then call "
            f"{submit_name} exactly once with {total_field!r} set to that sum and "
            f"{token_field!r} copied exactly from the tool result. Do not answer in text."
        )
        lookup_tool = {
            "type": "function",
            "function": {
                "name": lookup_name,
                "description": "Look up one ephemeral validator record.",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {lookup_field: {"type": "string"}},
                    "required": [lookup_field],
                    "additionalProperties": False,
                },
            },
        }
        submit_tool = {
            "type": "function",
            "function": {
                "name": submit_name,
                "description": "Submit the computed total and returned token.",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "properties": {
                        total_field: {"type": "integer"},
                        token_field: {"type": "string"},
                    },
                    "required": [total_field, token_field],
                    "additionalProperties": False,
                },
            },
        }
        steps = [
            {
                "tools": [lookup_tool],
                "tool_choice": {"type": "function", "function": {"name": lookup_name}},
                "expected_hash": _hash_text(first_expected),
            },
            {
                "tool_result": tool_result,
                "tools": [submit_tool],
                "tool_choice": {"type": "function", "function": {"name": submit_name}},
                "expected_hash": _hash_text(second_expected),
            },
        ]
        kind = "tool.chain"
        capability = "text.tool_chain.v1"
    elif selected == "stop.sequence":
        expected = secrets.token_hex(6).upper()
        stop = f"<STOP_{secrets.token_hex(5).upper()}>"
        suffix = secrets.token_hex(6).upper()
        prompt = (
            "Output the following characters exactly, with no spaces or explanation: "
            f"{expected}{stop}{suffix}"
        )
        kind = "stop.sequence"
        capability = "text.stop_sequence.v1"
    else:
        expected = secrets.token_hex(6).upper()
        challenge_max_tokens = secrets.randbelow(97) + 160
        prompt = (
            f"Repeat exactly {expected} separated only by single spaces. Continue "
            "repeating it until the generation limit stops you. Do not stop early, "
            "add punctuation, or output any other visible text."
        )
        kind = "token.limit"
        capability = "text.token_limit.v1"
    challenge = {
        "kind": kind,
        "prompt": prompt,
        "expected_hash": _hash_text(expected),
        "max_tokens": challenge_max_tokens,
        "temperature": 0,
        "capability": capability,
    }
    if selected == "tool.call":
        challenge["tools"] = tools
        challenge["tool_choice"] = tool_choice
    if selected == "tool.chain":
        challenge["steps"] = steps
    if selected == "stop.sequence":
        challenge["stop"] = stop
    if selected == "code.function":
        challenge["function_name"] = function_name
        challenge["test_inputs"] = test_inputs
    return challenge


def _text_generator_selector(canary_kind: str) -> str:
    """Map a stored concrete canary kind back to its randomized generator."""
    if canary_kind in {"math.add", "math.mul"}:
        return canary_kind
    if canary_kind in _TEXT_CHALLENGE_KINDS:
        return canary_kind
    raise AssignmentError("text probe group has an unsupported canary kind")


def _strip_think(text: str) -> str:
    return re.sub(
        r"<think(?:ing)?>.*?</think(?:ing)?>",
        "",
        text or "",
        flags=re.DOTALL,
    ).strip()


def _strip_wrapping_quotes(text: str) -> str:
    answer = (text or "").strip()
    wrappers = (("`", "`"), ('"', '"'), ("'", "'"))
    changed = True
    while changed and len(answer) >= 2:
        changed = False
        for left, right in wrappers:
            if answer.startswith(left) and answer.endswith(right):
                answer = answer[1:-1].strip()
                changed = True
                break
    return answer


def _normalized_tool_call(tool_calls: Any) -> str | None:
    if not isinstance(tool_calls, list) or len(tool_calls) != 1:
        return None
    call = tool_calls[0]
    if not isinstance(call, dict):
        return None
    function = call.get("function")
    if not isinstance(function, dict) or not isinstance(function.get("name"), str):
        return None
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return None
    if not isinstance(arguments, dict):
        return None
    return _canonical({"name": function["name"], "arguments": arguments})


def _normalized_tool_chain(tool_chain: Any) -> str | None:
    if not isinstance(tool_chain, list) or len(tool_chain) != 2:
        return None
    normalized: list[dict[str, Any]] = []
    for stage in tool_chain:
        if not isinstance(stage, dict) or _strip_think(str(stage.get("text") or "")):
            return None
        calls = stage.get("tool_calls")
        call = _normalized_tool_call(calls)
        if call is None:
            return None
        raw_call = calls[0]
        if not isinstance(raw_call.get("id"), str) or not raw_call["id"].strip():
            return None
        normalized.append(json.loads(call))
    return _canonical(normalized)


_CODE_BINARY_OPERATORS = {
    ast.Add: lambda left, right: left + right,
    ast.Sub: lambda left, right: left - right,
    ast.Mult: lambda left, right: left * right,
    ast.FloorDiv: lambda left, right: left // right,
    ast.Mod: lambda left, right: left % right,
}
_CODE_VALUE_LIMIT = 10**12


def _evaluate_code_expression(node: ast.AST, x: int) -> int:
    if isinstance(node, ast.Name) and node.id == "x":
        return x
    if isinstance(node, ast.Constant) and type(node.value) is int:
        if abs(node.value) > 1_000_000:
            raise ValueError("integer literal is out of bounds")
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _evaluate_code_expression(node.operand, x)
        result = value if isinstance(node.op, ast.UAdd) else -value
    elif isinstance(node, ast.BinOp) and type(node.op) in _CODE_BINARY_OPERATORS:
        left = _evaluate_code_expression(node.left, x)
        right = _evaluate_code_expression(node.right, x)
        if isinstance(node.op, (ast.FloorDiv, ast.Mod)) and right == 0:
            raise ValueError("division by zero")
        result = _CODE_BINARY_OPERATORS[type(node.op)](left, right)
    else:
        raise ValueError("unsupported code expression")
    if abs(result) > _CODE_VALUE_LIMIT:
        raise ValueError("code result is out of bounds")
    return result


def _normalized_code_answer(challenge: dict[str, Any], text: str) -> str | None:
    """Interpret a tiny arithmetic Python subset; never execute worker code."""
    answer = _strip_think(text)
    if not answer or len(answer.encode("utf-8")) > 4_096:
        return None
    function_name = challenge.get("function_name")
    test_inputs = challenge.get("test_inputs")
    if (
        not isinstance(function_name, str)
        or not re.fullmatch(r"transform_[0-9a-f]{8}", function_name)
        or not isinstance(test_inputs, list)
        or not 3 <= len(test_inputs) <= 16
        or any(type(value) is not int or abs(value) > 1_000_000 for value in test_inputs)
    ):
        return None
    try:
        tree = ast.parse(answer, mode="exec")
    except (SyntaxError, TypeError, ValueError):
        return None
    if len(list(ast.walk(tree))) > 64 or len(tree.body) != 1:
        return None
    function = tree.body[0]
    if not isinstance(function, ast.FunctionDef) or function.name != function_name:
        return None
    args = function.args
    if (
        function.decorator_list
        or function.returns is not None
        or args.posonlyargs
        or len(args.args) != 1
        or args.args[0].arg != "x"
        or args.args[0].annotation is not None
        or args.vararg is not None
        or args.kwonlyargs
        or args.kwarg is not None
        or args.defaults
        or args.kw_defaults
        or len(function.body) != 1
        or not isinstance(function.body[0], ast.Return)
    ):
        return None
    try:
        outputs = [
            _evaluate_code_expression(function.body[0].value, value)
            for value in test_inputs
        ]
    except (ArithmeticError, TypeError, ValueError):
        return None
    return _canonical(outputs)


def _normalized_text_answer(
    kind: str,
    text: str,
    tool_calls: Any = None,
    tool_chain: Any = None,
) -> str | None:
    answer = _strip_think(text)
    if kind == "tool.call":
        if answer:
            return None
        return _normalized_tool_call(tool_calls)
    if kind == "tool.chain":
        return _normalized_tool_chain(tool_chain)
    if not answer:
        return None
    if kind in (
        "echo",
        "context.retrieve",
        "context.retrieve.16k",
        "context.retrieve.32k",
        "stop.sequence",
    ):
        candidate = _strip_wrapping_quotes(answer)
        return candidate if candidate and not re.search(r"\s", candidate) else None
    if kind == "json.object":
        try:
            parsed = json.loads(answer)
        except (TypeError, ValueError):
            return None
        return _canonical(parsed) if isinstance(parsed, dict) else None
    if kind.startswith("math.") or kind == "logic.steps":
        numbers = re.findall(r"(?<![a-z0-9-])-?\d+(?![a-z0-9])", answer.lower())
        return numbers[0] if len(numbers) == 1 else None
    return None


def _normalized_token_limit_answer(
    challenge: dict[str, Any],
    text: str,
    reasoning_text: str,
    finish_reason: str | None,
) -> str | None:
    """Verify gross output-budget compliance without claiming native-token parity."""
    try:
        max_tokens = int(challenge.get("max_tokens") or 0)
    except (TypeError, ValueError):
        return None
    if max_tokens < 32 or finish_reason not in {"length", "max_tokens"}:
        return None

    answer = _strip_think(text)
    pieces = answer.split()
    if len(pieces) < 2 or any(piece != pieces[0] for piece in pieces):
        return None

    from .den import count_tokens

    observed = count_tokens(text) + count_tokens(reasoning_text)
    minimum = max(1, max_tokens // 2)
    maximum = ((max_tokens * 5) + 3) // 4 + 8
    if observed < minimum or observed > maximum:
        return None
    return pieces[0]


def _score_text_challenge(
    challenge: dict[str, Any],
    text: str,
    latency_ms: int,
    *,
    tool_calls: Any = None,
    tool_chain: Any = None,
    reasoning_text: str = "",
    finish_reason: str | None = None,
) -> str:
    expected_hash = str(challenge.get("expected_hash") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        return "failed"
    kind = str(challenge.get("kind") or "")
    if kind == "token.limit":
        candidate = _normalized_token_limit_answer(
            challenge,
            text,
            reasoning_text,
            finish_reason,
        )
    elif kind == "code.function":
        candidate = _normalized_code_answer(challenge, text)
    else:
        candidate = _normalized_text_answer(kind, text, tool_calls, tool_chain)
    if candidate is None:
        return "failed"
    if not secrets.compare_digest(_hash_text(candidate), expected_hash):
        return "failed"
    if latency_ms > PROBE_LATENCY_BUDGET_SECONDS * 1000:
        return "slow"
    return "healthy"


def _assignment_to_dict(
    row,
    *,
    include_challenge: bool = True,
    include_grid_nonce: bool = True,
    sealed: bool | None = None,
) -> dict[str, Any]:
    if sealed is None:
        sealed = bool(
            getattr(get_settings(), "validator_sealed_assignments_enabled", False),
        )
    if sealed:
        return {
            "assignment_id": row["id"],
            "modality": row["modality"],
            "capability": row["capability"],
            "scoring_policy_id": row["scoring_policy_id"],
            "score_dimension": _score_dimension(row["modality"], row["capability"]),
            "quality_eligible": _quality_eligible(row["modality"], row["capability"]),
            "status": row["status"],
            "probe_status": row["probe_status"],
            "probe_attempts": int(row["probe_attempts"] or 0),
            "created": row["created"].isoformat() if row["created"] else None,
            "expires": row["expires"].isoformat() if row["expires"] else None,
            "sealed": True,
            "assignment_seal": _assignment_seal(row),
        }
    out = {
        "assignment_id": row["id"],
        "probe_group_id": row.get("probe_group_id"),
        "target_worker_id": row["target_worker_id"],
        "target_worker_name": row["target_worker_name"],
        "model": row["model"],
        "modality": row["modality"],
        "capability": row["capability"],
        "canary_kind": row["canary_kind"],
        "scoring_policy_id": row["scoring_policy_id"],
        "worker_compensation": row.get("worker_compensation") or "none",
        "score_dimension": _score_dimension(row["modality"], row["capability"]),
        "quality_eligible": _quality_eligible(row["modality"], row["capability"]),
        "status": row["status"],
        "quorum_status": row["quorum_status"],
        "quorum_outcome": row["quorum_outcome"],
        "probe_status": row["probe_status"],
        "probe_attempts": int(row["probe_attempts"] or 0),
        "probe_job_id": row["probe_job_id"],
        "created": row["created"].isoformat() if row["created"] else None,
        "expires": row["expires"].isoformat() if row["expires"] else None,
        "probed": row["probed"].isoformat() if row["probed"] else None,
        "finalized": row["finalized"].isoformat() if row["finalized"] else None,
        "sealed": False,
    }
    if include_grid_nonce:
        out["grid_nonce"] = row["grid_nonce"]
    if include_challenge:
        challenge = row["challenge"] or {}
        keys = (
            (
                "schema", "kind", "modality", "prompt", "seed", "model",
                "model_digest", "recipe_id", "recipe_root", "parameters",
                "reference_worker_ids", "scoring_policy_id",
            )
            if row["modality"] in {"image", "video"}
            else (
                "kind", "prompt", "expected_hash", "max_tokens", "temperature",
                "tools", "tool_choice", "steps", "stop", "function_name",
                "test_inputs",
            )
        )
        out["challenge"] = {key: challenge[key] for key in keys if key in challenge}
    return out


def _assignment_seal_payload(row) -> dict[str, Any]:
    """Return the immutable assignment fields hidden until probe completion."""
    return {
        "schema": "aipg.validator.assignment.seal.v1",
        "assignment_id": str(row["id"]),
        "probe_group_id": str(row.get("probe_group_id") or ""),
        "grid_nonce": str(row["grid_nonce"]),
        "target_worker_id": str(row["target_worker_id"]),
        "model": str(row["model"]),
        "modality": str(row["modality"]),
        "capability": str(row["capability"]),
        "canary_kind": str(row["canary_kind"]),
        "scoring_policy_id": str(row["scoring_policy_id"]),
        "challenge": row.get("challenge") or {},
    }


def _assignment_seal(row) -> str:
    return _hash_obj(_assignment_seal_payload(row))


def _assignment_disclosure(row) -> dict[str, Any]:
    """Reveal the sealed fields only in the terminal probe response."""
    return {
        "assignment_seal": _assignment_seal(row),
        "probe_group_id": str(row.get("probe_group_id") or ""),
        "grid_nonce": str(row["grid_nonce"]),
        "target_worker_id": str(row["target_worker_id"]),
        "model": str(row["model"]),
        "modality": str(row["modality"]),
        "capability": str(row["capability"]),
        "canary_kind": str(row["canary_kind"]),
        "scoring_policy_id": str(row["scoring_policy_id"]),
        "challenge": row.get("challenge") or {},
    }


async def _hydrate_assignment_challenges(session, rows) -> list[dict[str, Any]]:
    """Resolve grouped assignments from the probe group's single challenge copy."""
    hydrated = [dict(row) for row in rows]
    group_ids = {
        str(row["probe_group_id"])
        for row in hydrated
        if row.get("probe_group_id") and not (row.get("challenge") or {})
    }
    if not group_ids:
        return hydrated
    group_rows = (
        await session.execute(
            sa.select(probe_groups_t.c.id, probe_groups_t.c.challenge).where(
                probe_groups_t.c.id.in_(group_ids)
            )
        )
    ).mappings().all()
    challenges = {str(row["id"]): row["challenge"] or {} for row in group_rows}
    for row in hydrated:
        group_id = row.get("probe_group_id")
        if group_id and not (row.get("challenge") or {}):
            row["challenge"] = challenges.get(str(group_id), {})
    return hydrated


def _assignment_available_for_validator(validator_id: str, now: datetime):
    """Select new work or a completed result still awaiting this validator's vote."""
    already_attested = sa.exists(
        sa.select(attestations_t.c.id).where(
            attestations_t.c.assignment_id == assignments_t.c.id,
            attestations_t.c.validator_id == validator_id,
            attestations_t.c.authority == "authoritative",
        )
    )
    return sa.or_(
        sa.and_(
            assignments_t.c.probe_status != "completed",
            assignments_t.c.expires >= now,
        ),
        sa.and_(
            assignments_t.c.probe_status == "completed",
            assignments_t.c.probe_result.isnot(None),
            assignments_t.c.expires
            >= now - timedelta(seconds=ATTESTATION_GRACE_SECONDS),
            ~already_attested,
        ),
    )


async def _finalize_due_assignments(session) -> None:
    now = _now()
    group_deadline = now - timedelta(seconds=ATTESTATION_GRACE_SECONDS)
    groups = (
        await session.execute(
            sa.select(probe_groups_t).where(
                probe_groups_t.c.expires < group_deadline,
                probe_groups_t.c.quorum_status != "finalized",
            )
        )
    ).mappings().all()
    for group in groups:
        votes = (
            await session.execute(
                sa.select(attestations_t.c.verdict, sa.func.count().label("count"))
                .where(
                    attestations_t.c.probe_group_id == group["id"],
                    attestations_t.c.authority == "authoritative",
                    attestations_t.c.validator_id.isnot(None),
                )
                .group_by(attestations_t.c.verdict)
            )
        ).mappings().all()
        counts = {row["verdict"]: int(row["count"]) for row in votes}
        threshold = int(group["quorum_threshold"] or QUORUM_MIN)
        winners = [verdict for verdict, count in counts.items() if count >= threshold]
        if len(winners) == 1:
            outcome = winners[0]
        elif len(counts) > 1:
            outcome = "disputed"
        else:
            outcome = "insufficient_evidence"
        await session.execute(
            sa.update(probe_groups_t)
            .where(probe_groups_t.c.id == group["id"])
            .values(
                status="finalized",
                quorum_status="finalized",
                quorum_outcome=outcome,
                finalized=now,
            )
        )
        await session.execute(
            sa.update(assignments_t)
            .where(assignments_t.c.probe_group_id == group["id"])
            .values(
                status="finalized",
                quorum_status="finalized",
                quorum_outcome=outcome,
                finalized=now,
            )
        )
        await session.execute(
            sa.update(attestations_t)
            .where(attestations_t.c.probe_group_id == group["id"])
            .values(quorum_status="finalized")
        )

    # Legacy preview assignments from before shared probe groups still expire,
    # but they can never be promoted into multi-validator quorum.
    rows = (
        await session.execute(
            sa.select(
                assignments_t.c.id,
                assignments_t.c.quorum_status,
                assignments_t.c.quorum_outcome,
            ).where(
                assignments_t.c.probe_group_id.is_(None),
                assignments_t.c.expires < now,
                assignments_t.c.quorum_status != "finalized",
            )
        )
    ).mappings().all()
    for row in rows:
        outcome = row["quorum_outcome"] or (
            row["quorum_status"] if row["quorum_status"] in ("accepted", "disputed") else "no_evidence"
        )
        await session.execute(
            sa.update(assignments_t)
            .where(assignments_t.c.id == row["id"])
            .values(
                status="finalized",
                quorum_status="finalized",
                quorum_outcome=outcome,
                finalized=now,
            )
        )


async def prune_validator_operational_history(
    *,
    older_than_days: int | None = None,
) -> dict[str, int]:
    """Prune finalized assignment/group machinery, never signed evidence."""
    configured = get_settings().validator_history_retention_days
    retention_days = max(1, int(older_than_days or configured))
    cutoff = _now() - timedelta(days=retention_days)
    async with await new_session() as session:
        deleted_assignments = await session.execute(
            sa.delete(assignments_t).where(
                assignments_t.c.finalized.isnot(None),
                assignments_t.c.finalized < cutoff,
            )
        )
        remaining_assignments = sa.select(assignments_t.c.id).where(
            assignments_t.c.probe_group_id == probe_groups_t.c.id
        )
        deleted_groups = await session.execute(
            sa.delete(probe_groups_t).where(
                probe_groups_t.c.finalized.isnot(None),
                probe_groups_t.c.finalized < cutoff,
                ~sa.exists(remaining_assignments),
            )
        )
        await session.commit()
    return {
        "assignments": max(0, int(deleted_assignments.rowcount or 0)),
        "probe_groups": max(0, int(deleted_groups.rowcount or 0)),
    }


async def issue_assignments(
    *,
    account_id,
    validator_id: str,
    validator_wallet: str | None,
    active_workers: list[dict[str, Any]],
    limit: int = 5,
    modality: str = "text",
) -> dict[str, Any]:
    """Return this validator's work from shared evidence-only probe groups.

    Optional paid-audit mode compensates the target worker from a bounded
    network budget. It does not reward validators or give evidence authority.
    """
    safe_limit = max(1, min(int(limit), 25))
    if modality == "image":
        return await _issue_image_assignments(
            account_id=account_id,
            validator_id=validator_id,
            validator_wallet=validator_wallet,
            active_workers=active_workers,
            limit=safe_limit,
        )
    if modality == "video":
        return await _issue_video_assignments(
            account_id=account_id,
            validator_id=validator_id,
            validator_wallet=validator_wallet,
            active_workers=active_workers,
            limit=safe_limit,
        )
    if modality != "text":
        raise AssignmentError("unsupported validator assignment modality")

    now = _now()
    expires = now + timedelta(seconds=ASSIGNMENT_TTL_SECONDS)
    wallet = validator_wallet.lower() if validator_wallet and _ADDR_RE.match(validator_wallet) else None
    from . import validator_audits

    try:
        worker_compensation = validator_audits.assignment_compensation(wallet)
    except validator_audits.AuditBudgetError as exc:
        raise AssignmentError(str(exc)) from exc

    async with await new_session() as session:
        # Serialize concurrent polls from the same registered validator. The DB
        # uniqueness guard remains the final protection on group membership.
        validator_row = (
            await session.execute(
                sa.select(
                    validators_t.c.id,
                    validators_t.c.capabilities,
                    validators_t.c.operator_group_id,
                )
                .where(validators_t.c.id == validator_id)
                .with_for_update()
            )
        ).mappings().first()
        if not validator_row:
            raise AssignmentError("active validator registration required")
        challenge_kinds, supported_capabilities = _supported_text_challenges(
            validator_row["capabilities"]
        )
        if not challenge_kinds:
            raise AssignmentError("validator has no supported text challenge capability")
        await _finalize_due_assignments(session)

        own_worker_rows = (
            await session.execute(
                sa.select(workers_t.c.id).where(workers_t.c.account_id == account_id)
            )
        ).all()
        own_worker_ids = {str(r[0]) for r in own_worker_rows}

        existing = (
            await session.execute(
                sa.select(assignments_t)
                .where(
                    assignments_t.c.account_id == account_id,
                    assignments_t.c.validator_id == validator_id,
                    assignments_t.c.modality == "text",
                    assignments_t.c.status != "finalized",
                    _assignment_available_for_validator(validator_id, now),
                    assignments_t.c.worker_compensation == worker_compensation,
                )
                .order_by(assignments_t.c.created.asc())
                .limit(safe_limit)
            )
        ).mappings().all()
        existing = await _hydrate_assignment_challenges(session, existing)
        existing_keys = {(r["target_worker_id"], r["model"]) for r in existing}
        rows = list(existing)

        for worker in active_workers:
            if len(rows) >= safe_limit:
                break
            worker_id = str(worker.get("worker_id") or worker.get("id") or "")
            worker_name = str(worker.get("name") or "")
            if not worker_id or not worker_name or worker_id in own_worker_ids:
                continue
            if modality not in (worker.get("job_types") or ["text"]):
                continue
            models = list(dict.fromkeys(
                m for m in (worker.get("models") or []) if isinstance(m, str) and m
            ))
            if not models:
                continue
            candidate_models = [
                model for model in models if (worker_id, model) not in existing_keys
            ]
            if not candidate_models:
                continue
            eligible_challenge_kinds = _worker_eligible_text_challenges(
                challenge_kinds,
                worker,
            )
            if not eligible_challenge_kinds:
                continue

            if session.bind and session.bind.dialect.name == "postgresql":
                lock_key = int.from_bytes(
                    hashlib.sha256(
                        f"validator-worker:{worker_id}:{modality}".encode()
                    ).digest()[:8],
                    byteorder="big",
                    signed=True,
                )
                await session.execute(
                    sa.text("SELECT pg_advisory_xact_lock(:lock_key)"),
                    {"lock_key": lock_key},
                )

            candidate_groups = (
                await session.execute(
                    sa.select(probe_groups_t)
                    .where(
                        probe_groups_t.c.target_worker_id == worker_id,
                        probe_groups_t.c.model.in_(candidate_models),
                        probe_groups_t.c.modality == modality,
                        probe_groups_t.c.expires >= now,
                        probe_groups_t.c.quorum_status != "finalized",
                    )
                    .order_by(probe_groups_t.c.created.asc())
                )
            ).mappings().all()
            group = None
            model = None
            blocked_models: set[str] = set()
            for candidate in candidate_groups:
                if candidate["capability"] not in supported_capabilities:
                    continue
                if not _worker_supports_text_capability(
                    candidate["capability"],
                    worker,
                ):
                    continue
                assigned_count = int(
                    await session.scalar(
                        sa.select(sa.func.count())
                        .select_from(assignments_t)
                        .where(assignments_t.c.probe_group_id == candidate["id"])
                    )
                    or 0
                )
                if assigned_count >= int(candidate["target_validator_count"]):
                    continue
                already_assigned = await _operator_already_assigned(
                    session,
                    probe_group_id=str(candidate["id"]),
                    validator_id=validator_id,
                    operator_group_id=validator_row["operator_group_id"],
                )
                if not already_assigned:
                    group = candidate
                    model = str(candidate["model"])
                    break
                blocked_models.add(str(candidate["model"]))

            # Do not let one fast validator manufacture many nominally shared
            # groups for the same model while its current group waits for peers.
            # It may still cover another model advertised by the same worker.

            if group is None:
                coverage_rows = (
                    await session.execute(
                        sa.select(
                            probe_groups_t.c.model,
                            sa.func.max(probe_groups_t.c.created).label("last_created"),
                        )
                        .where(
                            probe_groups_t.c.target_worker_id == worker_id,
                            probe_groups_t.c.modality == modality,
                            probe_groups_t.c.model.in_(candidate_models),
                        )
                        .group_by(probe_groups_t.c.model)
                    )
                ).all()
                last_created = {
                    str(row[0]): _aware(row[1]).timestamp()
                    for row in coverage_rows
                    if row[1] is not None
                }
                advertised_order = {name: index for index, name in enumerate(models)}
                ordered_models = sorted(
                    candidate_models,
                    key=lambda name: (
                        last_created.get(name, float("-inf")),
                        advertised_order[name],
                    ),
                )
                for candidate_model in ordered_models:
                    if candidate_model in blocked_models:
                        continue
                    if await _text_group_cooldown_active(
                        session,
                        worker_id=worker_id,
                        model=candidate_model,
                        now=now,
                    ):
                        continue
                    model = candidate_model
                    break
                if model is None:
                    continue
                challenge = _make_text_challenge(secrets.choice(eligible_challenge_kinds))
                group_id = f"prg_{uuid4().hex}"
                batch_contract = {
                    "schema": "aipg.validator.text.batch.v1",
                    "generator_kind": _text_generator_selector(challenge["kind"]),
                    "capability": challenge["capability"],
                    "score_dimension": _score_dimension("text", challenge["capability"]),
                    "quality_eligible": False,
                }
                group_values = {
                    "id": group_id,
                    "target_worker_id": worker_id,
                    "target_worker_name": worker_name,
                    "model": model,
                    "modality": "text",
                    "capability": challenge["capability"],
                    "canary_kind": challenge["kind"],
                    "scoring_policy_id": _TEXT_BATCH_SCORING_POLICY,
                    "challenge": batch_contract,
                    "challenge_hash": _hash_obj({
                        "group_id": group_id,
                        "worker_id": worker_id,
                        "model": model,
                        "challenge": batch_contract,
                    }),
                    "status": "pending",
                    "quorum_status": "pending",
                    "quorum_outcome": None,
                    "quorum_threshold": QUORUM_MIN,
                    "target_validator_count": QUORUM_TARGET,
                    "created": now,
                    "expires": expires,
                    "accepted": None,
                    "disputed": None,
                    "finalized": None,
                }
                await session.execute(sa.insert(probe_groups_t).values(**group_values))
                group = group_values
            else:
                if group["scoring_policy_id"] == _TEXT_BATCH_SCORING_POLICY:
                    challenge = _make_text_challenge(
                        _text_generator_selector(str(group["canary_kind"])),
                    )
                    if (
                        challenge["capability"] != group["capability"]
                        or challenge["kind"] != group["canary_kind"]
                    ):
                        raise AssignmentError("generated challenge does not match probe batch")
                else:
                    # Rollout compatibility: finish already-open v7 groups with
                    # their original shared challenge, then create only v8.
                    challenge = group["challenge"] or {}

            assignment_id = f"asg_{uuid4().hex}"
            grid_nonce = secrets.token_urlsafe(24)
            values = {
                "id": assignment_id,
                "probe_group_id": group["id"],
                "account_id": account_id,
                "validator_id": validator_id,
                "validator_wallet": wallet,
                "grid_nonce": grid_nonce,
                "target_worker_id": worker_id,
                "target_worker_name": worker_name,
                "model": model,
                "modality": "text",
                "capability": group["capability"],
                "canary_kind": group["canary_kind"],
                "scoring_policy_id": group["scoring_policy_id"],
                "worker_compensation": worker_compensation,
                "challenge": challenge,
                "status": "pending",
                "quorum_status": "pending",
                "quorum_outcome": None,
                "probe_job_id": None,
                "probe_status": "not_started",
                "probe_attempts": 0,
                "probe_lease_expires": None,
                "created": now,
                "expires": group["expires"],
                "probed": None,
                "finalized": None,
            }
            await session.execute(
                sa.insert(assignments_t).values(
                    **{
                        **values,
                        "challenge": (
                            challenge
                            if group["scoring_policy_id"] == _TEXT_BATCH_SCORING_POLICY
                            else {}
                        ),
                    },
                ),
            )
            rows.append(values)
            existing_keys.add((worker_id, model))

        await session.commit()

    return {
        "assignments": [_assignment_to_dict(r) for r in rows[:safe_limit]],
        "count": min(len(rows), safe_limit),
        "targeted_probe_enabled": True,
        "quorum": await assignment_health(account_id=account_id),
        "quorum_policy": {
            "threshold": QUORUM_MIN,
            "target_validators": QUORUM_TARGET,
            "distinct_registered_validators": True,
        },
        "economic_effect": "none",
    }


async def _issue_image_assignments(
    *,
    account_id,
    validator_id: str,
    validator_wallet: str | None,
    active_workers: list[dict[str, Any]],
    limit: int,
) -> dict[str, Any]:
    """Allocate deterministic image-fidelity work only when every gate is live."""
    from . import validator_references

    policy = media_validation_policy()
    if not policy["enabled"]:
        raise AssignmentError("image validator assignments are not enabled")

    now = _now()
    expires = now + timedelta(seconds=ASSIGNMENT_TTL_SECONDS)
    wallet = validator_wallet.lower() if validator_wallet and _ADDR_RE.match(validator_wallet) else None
    async with await new_session() as session:
        validator_row = (
            await session.execute(
                sa.select(
                    validators_t.c.id,
                    validators_t.c.capabilities,
                    validators_t.c.operator_group_id,
                )
                .where(validators_t.c.id == validator_id)
                .with_for_update()
            )
        ).mappings().first()
        if not validator_row:
            raise AssignmentError("active validator registration required")
        supported = {str(value) for value in (validator_row["capabilities"] or [])}
        if "image.fidelity.v1" not in supported:
            raise AssignmentError("validator has no supported image fidelity capability")
        await _finalize_due_assignments(session)

        own_worker_ids = {
            str(row[0])
            for row in (
                await session.execute(
                    sa.select(workers_t.c.id).where(workers_t.c.account_id == account_id)
                )
            ).all()
        }
        existing = (
            await session.execute(
                sa.select(assignments_t).where(
                    assignments_t.c.account_id == account_id,
                    assignments_t.c.validator_id == validator_id,
                    assignments_t.c.modality == "image",
                    assignments_t.c.status != "finalized",
                    _assignment_available_for_validator(validator_id, now),
                ).order_by(assignments_t.c.created.asc()).limit(limit)
            )
        ).mappings().all()
        existing = await _hydrate_assignment_challenges(session, existing)
        rows = list(existing)
        existing_keys = {(str(row["target_worker_id"]), row["model"]) for row in existing}

        for worker in active_workers:
            if len(rows) >= limit:
                break
            worker_id = str(worker.get("worker_id") or worker.get("id") or "")
            worker_name = str(worker.get("name") or "")
            if (
                not worker_id
                or not worker_name
                or worker_id in own_worker_ids
                or "image" not in (worker.get("job_types") or [])
            ):
                continue
            for recipe in _image_recipe_for_worker(worker):
                if len(rows) >= limit:
                    break
                key = (worker_id, recipe.model_name)
                if key in existing_keys:
                    continue
                if session.bind and session.bind.dialect.name == "postgresql":
                    lock_key = int.from_bytes(
                        hashlib.sha256(
                            f"validator-group:{worker_id}:{recipe.model_name}:image".encode()
                        ).digest()[:8],
                        byteorder="big",
                        signed=True,
                    )
                    await session.execute(
                        sa.text("SELECT pg_advisory_xact_lock(:lock_key)"),
                        {"lock_key": lock_key},
                    )

                candidate_groups = (
                    await session.execute(
                        sa.select(probe_groups_t).where(
                            probe_groups_t.c.target_worker_id == worker_id,
                            probe_groups_t.c.model == recipe.model_name,
                            probe_groups_t.c.modality == "image",
                            probe_groups_t.c.capability == "image.fidelity.v1",
                            probe_groups_t.c.expires >= now,
                            probe_groups_t.c.quorum_status != "finalized",
                        ).order_by(probe_groups_t.c.created.asc())
                    )
                ).mappings().all()
                group = None
                unfilled_group_exists = False
                for candidate in candidate_groups:
                    assigned_count = int(
                        await session.scalar(
                            sa.select(sa.func.count()).select_from(assignments_t).where(
                                assignments_t.c.probe_group_id == candidate["id"]
                            )
                        ) or 0
                    )
                    if assigned_count >= int(candidate["target_validator_count"]):
                        continue
                    unfilled_group_exists = True
                    already_assigned = await _operator_already_assigned(
                        session,
                        probe_group_id=str(candidate["id"]),
                        validator_id=validator_id,
                        operator_group_id=validator_row["operator_group_id"],
                    )
                    if not already_assigned:
                        group = candidate
                        break
                if group is None and unfilled_group_exists:
                    continue

                if group is None:
                    required = set(recipe.required_models or [recipe.model_name])
                    online_ids = [
                        str(item.get("worker_id") or item.get("id") or "")
                        for item in active_workers
                        if "image" in (item.get("job_types") or [])
                        and required.issubset({str(value) for value in (item.get("models") or [])})
                    ]
                    try:
                        references = await validator_references.select_reference_workers(
                            session,
                            model=recipe.model_name,
                            modality="image",
                            candidate_worker_id=worker_id,
                            online_model_worker_ids=online_ids,
                            expected_chain_id=policy["chain_id"],
                            expected_bond_contract=policy["bond_contract"],
                            expected_verifier_version=policy["bond_verifier_version"],
                            minimum_bond_raw=policy["minimum_bond_raw"],
                            minimum_quality_pass_rate=policy["minimum_quality_pass_rate"],
                        )
                    except validator_references.ReferencePoolUnavailable:
                        continue
                    challenge = _make_image_challenge(
                        recipe,
                        [str(reference.worker_id) for reference in references],
                    )
                    group_id = f"prg_{uuid4().hex}"
                    group_values = {
                        "id": group_id,
                        "target_worker_id": worker_id,
                        "target_worker_name": worker_name,
                        "model": recipe.model_name,
                        "modality": "image",
                        "capability": "image.fidelity.v1",
                        "canary_kind": "image.fidelity",
                        "scoring_policy_id": "image.fidelity.v1",
                        "challenge": challenge,
                        "challenge_hash": _hash_obj({
                            "group_id": group_id,
                            "worker_id": worker_id,
                            "model": recipe.model_name,
                            "challenge": challenge,
                        }),
                        "status": "pending",
                        "quorum_status": "pending",
                        "quorum_outcome": None,
                        "quorum_threshold": QUORUM_MIN,
                        "target_validator_count": QUORUM_TARGET,
                        "created": now,
                        "expires": expires,
                        "accepted": None,
                        "disputed": None,
                        "finalized": None,
                    }
                    await session.execute(sa.insert(probe_groups_t).values(**group_values))
                    group = group_values
                challenge = group["challenge"] or {}
                values = {
                    "id": f"asg_{uuid4().hex}",
                    "probe_group_id": group["id"],
                    "account_id": account_id,
                    "validator_id": validator_id,
                    "validator_wallet": wallet,
                    "grid_nonce": secrets.token_urlsafe(24),
                    "target_worker_id": worker_id,
                    "target_worker_name": worker_name,
                    "model": recipe.model_name,
                    "modality": "image",
                    "capability": "image.fidelity.v1",
                    "canary_kind": "image.fidelity",
                    "scoring_policy_id": "image.fidelity.v1",
                    "challenge": challenge,
                    "status": "pending",
                    "quorum_status": "pending",
                    "quorum_outcome": None,
                    "probe_job_id": None,
                    "probe_status": "not_started",
                    "probe_attempts": 0,
                    "probe_lease_expires": None,
                    "created": now,
                    "expires": group["expires"],
                    "probed": None,
                    "finalized": None,
                }
                await session.execute(
                    sa.insert(assignments_t).values(**{**values, "challenge": {}})
                )
                rows.append(values)
                existing_keys.add(key)

        await session.commit()

    return {
        "assignments": [_assignment_to_dict(row) for row in rows[:limit]],
        "count": min(len(rows), limit),
        "targeted_probe_enabled": True,
        "quorum": await assignment_health(account_id=account_id),
        "quorum_policy": {
            "threshold": QUORUM_MIN,
            "target_validators": QUORUM_TARGET,
            "distinct_registered_validators": True,
        },
        "economic_effect": "none",
    }


async def _issue_video_assignments(
    *,
    account_id,
    validator_id: str,
    validator_wallet: str | None,
    active_workers: list[dict[str, Any]],
    limit: int,
) -> dict[str, Any]:
    """Allocate objective video-contract work only when every dark gate is live."""
    policy = video_validation_policy()
    if not policy["enabled"]:
        raise AssignmentError("video validator assignments are not enabled")

    now = _now()
    expires = now + timedelta(seconds=ASSIGNMENT_TTL_SECONDS)
    wallet = validator_wallet.lower() if validator_wallet and _ADDR_RE.match(validator_wallet) else None
    async with await new_session() as session:
        validator_row = (
            await session.execute(
                sa.select(
                    validators_t.c.id,
                    validators_t.c.capabilities,
                    validators_t.c.operator_group_id,
                )
                .where(validators_t.c.id == validator_id)
                .with_for_update()
            )
        ).mappings().first()
        if not validator_row:
            raise AssignmentError("active validator registration required")
        supported = {str(value) for value in (validator_row["capabilities"] or [])}
        if "video.contract.v1" not in supported:
            raise AssignmentError("validator has no supported video contract capability")
        await _finalize_due_assignments(session)

        own_worker_ids = {
            str(row[0])
            for row in (
                await session.execute(
                    sa.select(workers_t.c.id).where(workers_t.c.account_id == account_id)
                )
            ).all()
        }
        existing = (
            await session.execute(
                sa.select(assignments_t).where(
                    assignments_t.c.account_id == account_id,
                    assignments_t.c.validator_id == validator_id,
                    assignments_t.c.modality == "video",
                    assignments_t.c.status != "finalized",
                    _assignment_available_for_validator(validator_id, now),
                ).order_by(assignments_t.c.created.asc()).limit(limit)
            )
        ).mappings().all()
        existing = await _hydrate_assignment_challenges(session, existing)
        rows = list(existing)
        existing_keys = {(str(row["target_worker_id"]), row["model"]) for row in existing}

        for worker in active_workers:
            if len(rows) >= limit:
                break
            worker_id = str(worker.get("worker_id") or worker.get("id") or "")
            worker_name = str(worker.get("name") or "")
            if (
                not worker_id
                or not worker_name
                or worker_id in own_worker_ids
                or "video" not in (worker.get("job_types") or [])
            ):
                continue
            for recipe in _video_recipe_for_worker(worker):
                if len(rows) >= limit:
                    break
                key = (worker_id, recipe.model_name)
                if key in existing_keys:
                    continue
                if session.bind and session.bind.dialect.name == "postgresql":
                    lock_key = int.from_bytes(
                        hashlib.sha256(
                            f"validator-group:{worker_id}:{recipe.model_name}:video".encode()
                        ).digest()[:8],
                        byteorder="big",
                        signed=True,
                    )
                    await session.execute(
                        sa.text("SELECT pg_advisory_xact_lock(:lock_key)"),
                        {"lock_key": lock_key},
                    )

                candidate_groups = (
                    await session.execute(
                        sa.select(probe_groups_t).where(
                            probe_groups_t.c.target_worker_id == worker_id,
                            probe_groups_t.c.model == recipe.model_name,
                            probe_groups_t.c.modality == "video",
                            probe_groups_t.c.capability == "video.contract.v1",
                            probe_groups_t.c.expires >= now,
                            probe_groups_t.c.quorum_status != "finalized",
                        ).order_by(probe_groups_t.c.created.asc())
                    )
                ).mappings().all()
                group = None
                unfilled_group_exists = False
                for candidate in candidate_groups:
                    assigned_count = int(
                        await session.scalar(
                            sa.select(sa.func.count()).select_from(assignments_t).where(
                                assignments_t.c.probe_group_id == candidate["id"]
                            )
                        ) or 0
                    )
                    if assigned_count >= int(candidate["target_validator_count"]):
                        continue
                    unfilled_group_exists = True
                    already_assigned = await _operator_already_assigned(
                        session,
                        probe_group_id=str(candidate["id"]),
                        validator_id=validator_id,
                        operator_group_id=validator_row["operator_group_id"],
                    )
                    if not already_assigned:
                        group = candidate
                        break
                if group is None and unfilled_group_exists:
                    continue

                if group is None:
                    challenge = _make_video_challenge(recipe)
                    group_id = f"prg_{uuid4().hex}"
                    group_values = {
                        "id": group_id,
                        "target_worker_id": worker_id,
                        "target_worker_name": worker_name,
                        "model": recipe.model_name,
                        "modality": "video",
                        "capability": "video.contract.v1",
                        "canary_kind": "video.contract",
                        "scoring_policy_id": "video.contract.v1",
                        "challenge": challenge,
                        "challenge_hash": _hash_obj({
                            "group_id": group_id,
                            "worker_id": worker_id,
                            "model": recipe.model_name,
                            "challenge": challenge,
                        }),
                        "status": "pending",
                        "quorum_status": "pending",
                        "quorum_outcome": None,
                        "quorum_threshold": QUORUM_MIN,
                        "target_validator_count": QUORUM_TARGET,
                        "created": now,
                        "expires": expires,
                        "accepted": None,
                        "disputed": None,
                        "finalized": None,
                    }
                    await session.execute(sa.insert(probe_groups_t).values(**group_values))
                    group = group_values
                challenge = group["challenge"] or {}
                values = {
                    "id": f"asg_{uuid4().hex}",
                    "probe_group_id": group["id"],
                    "account_id": account_id,
                    "validator_id": validator_id,
                    "validator_wallet": wallet,
                    "grid_nonce": secrets.token_urlsafe(24),
                    "target_worker_id": worker_id,
                    "target_worker_name": worker_name,
                    "model": recipe.model_name,
                    "modality": "video",
                    "capability": "video.contract.v1",
                    "canary_kind": "video.contract",
                    "scoring_policy_id": "video.contract.v1",
                    "challenge": challenge,
                    "status": "pending",
                    "quorum_status": "pending",
                    "quorum_outcome": None,
                    "probe_job_id": None,
                    "probe_status": "not_started",
                    "probe_attempts": 0,
                    "probe_lease_expires": None,
                    "created": now,
                    "expires": group["expires"],
                    "probed": None,
                    "finalized": None,
                }
                await session.execute(
                    sa.insert(assignments_t).values(**{**values, "challenge": {}})
                )
                rows.append(values)
                existing_keys.add(key)

        await session.commit()

    return {
        "assignments": [_assignment_to_dict(row) for row in rows[:limit]],
        "count": min(len(rows), limit),
        "targeted_probe_enabled": True,
        "quorum": await assignment_health(account_id=account_id),
        "quorum_policy": {
            "threshold": QUORUM_MIN,
            "target_validators": QUORUM_TARGET,
            "distinct_registered_validators": True,
        },
        "economic_effect": "none",
    }


async def _network_health_in_session(session, *, since_hours: int) -> dict[str, Any]:
    """Return privacy-preserving aggregate validator network health."""
    now = _now()
    cutoff = now - timedelta(hours=since_hours)
    heartbeat_cutoff = now - timedelta(seconds=VALIDATOR_HEARTBEAT_FRESH_SECONDS)
    vote_rows = (
        await session.execute(
            sa.select(
                attestations_t.c.probe_group_id,
                attestations_t.c.verdict,
                sa.func.count().label("count"),
            )
            .where(
                attestations_t.c.authority == "authoritative",
                attestations_t.c.probe_group_id.isnot(None),
                attestations_t.c.validator_id.isnot(None),
                attestations_t.c.created >= cutoff,
            )
            .group_by(attestations_t.c.probe_group_id, attestations_t.c.verdict)
        )
    ).mappings().all()
    votes_by_group: dict[str, dict[str, int]] = {}
    for row in vote_rows:
        votes_by_group.setdefault(str(row["probe_group_id"]), {})[
            str(row["verdict"])
        ] = int(row["count"])

    group_ids = list(votes_by_group)
    group_meta = []
    if group_ids:
        group_meta = (
            await session.execute(
                sa.select(
                    probe_groups_t.c.id,
                    probe_groups_t.c.target_worker_id,
                    probe_groups_t.c.model,
                ).where(probe_groups_t.c.id.in_(group_ids))
            )
        ).mappings().all()

    total_votes = sum(sum(counts.values()) for counts in votes_by_group.values())
    plurality_votes = sum(max(counts.values()) for counts in votes_by_group.values())
    disputed_groups = sum(1 for counts in votes_by_group.values() if len(counts) > 1)
    evidence_groups = len(votes_by_group)
    completed_assignments = int(
        await session.scalar(
            sa.select(sa.func.count())
            .select_from(assignments_t)
            .where(
                assignments_t.c.probe_status == "completed",
                assignments_t.c.created >= cutoff,
            )
        )
        or 0
    )
    version_rows = (
        await session.execute(
            sa.select(
                validators_t.c.software_version,
                sa.func.count().label("count"),
            )
            .where(
                validators_t.c.status == "active",
                validators_t.c.last_heartbeat >= heartbeat_cutoff,
            )
            .group_by(validators_t.c.software_version)
            .order_by(sa.func.count().desc(), validators_t.c.software_version.asc())
            .limit(20)
        )
    ).mappings().all()
    verified_filter = sa.and_(
        validators_t.c.status == "active",
        validators_t.c.last_heartbeat >= heartbeat_cutoff,
        validators_t.c.operator_group_id.isnot(None),
        validators_t.c.independence_status == "verified",
        validators_t.c.independence_reviewed_at.isnot(None),
        validators_t.c.independence_expires_at >= now,
    )
    verified_operators = int(
        await session.scalar(
            sa.select(sa.func.count(sa.distinct(validators_t.c.operator_group_id))).where(
                verified_filter
            )
        )
        or 0
    )
    participating_operators = int(
        await session.scalar(
            sa.select(sa.func.count(sa.distinct(validators_t.c.operator_group_id)))
            .select_from(
                validators_t.join(
                    attestations_t,
                    attestations_t.c.validator_id == validators_t.c.id,
                )
            )
            .where(
                verified_filter,
                attestations_t.c.authority == "authoritative",
                attestations_t.c.created >= cutoff,
            )
        )
        or 0
    )
    independence_proven = verified_operators >= QUORUM_MIN

    return {
        "window_hours": since_hours,
        "assignments_completed": completed_assignments,
        "groups_with_evidence": evidence_groups,
        "authoritative_votes": total_votes,
        "agreement_rate": (plurality_votes / total_votes) if total_votes else None,
        "disputed_rate": (disputed_groups / evidence_groups) if evidence_groups else None,
        "disputed_groups": disputed_groups,
        "coverage": {
            "workers": len({str(row["target_worker_id"]) for row in group_meta}),
            "models": len({str(row["model"]) for row in group_meta}),
        },
        "software_versions": [
            {"version": str(row["software_version"]), "validators": int(row["count"])}
            for row in version_rows
        ],
        "operator_independence": {
            "verified": verified_operators,
            "participating": participating_operators,
            "proven": independence_proven,
            "status": (
                "quorum_available"
                if independence_proven
                else "below_quorum"
                if verified_operators
                else "not_yet_verified"
            ),
            "minimum": QUORUM_MIN,
        },
    }


async def public_health(*, since_hours: int = 24) -> dict[str, Any]:
    """Return redacted validator-network aggregates for public status.

    This deliberately omits assignments, wallets, account IDs, validator IDs,
    worker names, nonces, signatures, prompts, and evidence. Registration is
    never presented as proof that operators are independent.
    """
    safe_since = max(1, min(int(since_hours), 24 * 90))
    cutoff = _now() - timedelta(hours=safe_since)
    heartbeat_cutoff = _now() - timedelta(seconds=VALIDATOR_HEARTBEAT_FRESH_SECONDS)
    async with await new_session() as session:
        validator_row = (
            await session.execute(
                sa.select(
                    sa.func.count().filter(validators_t.c.status == "active").label("active"),
                    sa.func.count()
                    .filter(
                        validators_t.c.status == "active",
                        validators_t.c.last_heartbeat >= heartbeat_cutoff,
                    )
                    .label("fresh"),
                )
            )
        ).mappings().one()
        participating = int(
            await session.scalar(
                sa.select(sa.func.count(sa.distinct(attestations_t.c.validator_id))).where(
                    attestations_t.c.authority == "authoritative",
                    attestations_t.c.validator_id.isnot(None),
                    attestations_t.c.created >= cutoff,
                )
            )
            or 0
        )
        quorum_rows = (
            await session.execute(
                sa.select(
                    probe_groups_t.c.quorum_status,
                    sa.func.count().label("count"),
                )
                .where(probe_groups_t.c.created >= cutoff)
                .group_by(probe_groups_t.c.quorum_status)
            )
        ).mappings().all()
        network = await _network_health_in_session(session, since_hours=safe_since)

    quorum = {str(row["quorum_status"]): int(row["count"]) for row in quorum_rows}
    return {
        "window_hours": safe_since,
        "registered_active": int(validator_row["active"] or 0),
        "heartbeat_fresh": int(validator_row["fresh"] or 0),
        "participating": participating,
        "verified_independent": network["operator_independence"]["verified"],
        "participating_independent": network["operator_independence"]["participating"],
        "independence_proven": network["operator_independence"]["proven"],
        "quorum": {
            "pending": quorum.get("pending", 0),
            "accepted": quorum.get("accepted", 0),
            "disputed": quorum.get("disputed", 0),
            "finalized": quorum.get("finalized", 0),
        },
        "assignments_completed": network["assignments_completed"],
        "authoritative_votes": network["authoritative_votes"],
        "agreement_rate": network["agreement_rate"],
        "disputed_rate": network["disputed_rate"],
        "coverage": network["coverage"],
        "software_versions": network["software_versions"],
        "economic_effect": "none",
    }


async def assignment_health(
    *,
    account_id=None,
    limit: int = 25,
    since_hours: int = 24,
) -> dict[str, Any]:
    """Return probe, assignment, and real group-quorum health without raw evidence."""
    safe_limit = max(1, min(int(limit), 100))
    safe_since = max(1, min(int(since_hours), 24 * 90))
    now = _now()
    operator_fresh_cutoff = now - timedelta(seconds=VALIDATOR_HEARTBEAT_FRESH_SECONDS)
    async with await new_session() as session:
        await _finalize_due_assignments(session)
        await session.commit()
        group_scope = sa.select(assignments_t.c.probe_group_id).where(
            assignments_t.c.probe_group_id.isnot(None)
        )
        if account_id is not None:
            group_scope = group_scope.where(assignments_t.c.account_id == account_id)
        base = (
            sa.select(probe_groups_t.c.quorum_status, sa.func.count().label("count"))
            .where(probe_groups_t.c.id.in_(group_scope))
            .group_by(probe_groups_t.c.quorum_status)
        )
        quorum_counts = {
            row["quorum_status"]: int(row["count"])
            for row in (await session.execute(base)).mappings().all()
        }
        probe_q = sa.select(assignments_t.c.probe_status, sa.func.count().label("count")).group_by(
            assignments_t.c.probe_status
        )
        if account_id is not None:
            probe_q = probe_q.where(assignments_t.c.account_id == account_id)
        probe_counts = {
            row["probe_status"]: int(row["count"])
            for row in (await session.execute(probe_q)).mappings().all()
        }
        recent_q = (
            sa.select(probe_groups_t)
            .where(probe_groups_t.c.id.in_(group_scope))
            .order_by(probe_groups_t.c.created.desc())
            .limit(safe_limit)
        )
        recent_groups = (await session.execute(recent_q)).mappings().all()
        recent = []
        for group in recent_groups:
            assigned = int(
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(assignments_t)
                    .where(assignments_t.c.probe_group_id == group["id"])
                )
                or 0
            )
            attested = int(
                await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(attestations_t)
                    .where(
                        attestations_t.c.probe_group_id == group["id"],
                        attestations_t.c.authority == "authoritative",
                    )
                )
                or 0
            )
            independent_attested = int(
                await session.scalar(
                    sa.select(sa.func.count(sa.distinct(validators_t.c.operator_group_id)))
                    .select_from(
                        attestations_t.join(
                            validators_t,
                            validators_t.c.id == attestations_t.c.validator_id,
                        )
                    )
                    .where(
                        attestations_t.c.probe_group_id == group["id"],
                        attestations_t.c.authority == "authoritative",
                        validators_t.c.operator_group_id.isnot(None),
                        validators_t.c.status == "active",
                        validators_t.c.last_heartbeat >= operator_fresh_cutoff,
                        validators_t.c.independence_status == "verified",
                        validators_t.c.independence_reviewed_at.isnot(None),
                        validators_t.c.independence_expires_at >= now,
                    )
                )
                or 0
            )
            recent.append({
                "probe_group_id": group["id"],
                "target_worker_id": group["target_worker_id"],
                "model": group["model"],
                "modality": group["modality"],
                "capability": group["capability"],
                "quorum_status": group["quorum_status"],
                "quorum_outcome": group["quorum_outcome"],
                "assigned_validators": assigned,
                "attested_validators": attested,
                "independent_attested_operators": independent_attested,
                "independent_quorum_reached": independent_attested >= int(group["quorum_threshold"]),
                "threshold": int(group["quorum_threshold"]),
                "target_validators": int(group["target_validator_count"]),
                "created": group["created"].isoformat() if group["created"] else None,
                "expires": group["expires"].isoformat() if group["expires"] else None,
                "finalized": group["finalized"].isoformat() if group["finalized"] else None,
            })
        authoritative_evidence = int(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(attestations_t)
                .where(
                    attestations_t.c.probe_group_id.in_(group_scope),
                    attestations_t.c.authority == "authoritative",
                )
            )
            or 0
        )
        worker_passed = int(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(probe_groups_t)
                .where(
                    probe_groups_t.c.id.in_(group_scope),
                    probe_groups_t.c.quorum_outcome == "healthy",
                    probe_groups_t.c.quorum_status.in_(("accepted", "finalized")),
                )
            )
            or 0
        )
        quorum_reached = int(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(probe_groups_t)
                .where(
                    probe_groups_t.c.id.in_(group_scope),
                    probe_groups_t.c.quorum_outcome.in_(VALID_VERDICTS),
                    probe_groups_t.c.quorum_status.in_(("accepted", "finalized")),
                )
            )
            or 0
        )
        active_validators = int(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(validators_t)
                .where(validators_t.c.status == "active")
            )
            or 0
        )
        fresh_cutoff = _now() - timedelta(seconds=VALIDATOR_HEARTBEAT_FRESH_SECONDS)
        fresh_validators = int(
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(validators_t)
                .where(
                    validators_t.c.status == "active",
                    validators_t.c.last_heartbeat >= fresh_cutoff,
                )
            )
            or 0
        )
        participating_validators = int(
            await session.scalar(
                sa.select(sa.func.count(sa.distinct(attestations_t.c.validator_id)))
                .where(
                    attestations_t.c.authority == "authoritative",
                    attestations_t.c.validator_id.isnot(None),
                    attestations_t.c.created >= _now() - timedelta(hours=24),
                )
            )
            or 0
        )
        network_health = await _network_health_in_session(
            session,
            since_hours=safe_since,
        )
    from . import validator_audits

    audit_policy = validator_audits.public_policy()
    # Operational visibility survives rollback. Disabling new paid assignments
    # must not hide holds that still need settlement or release.
    audit_budget = await validator_audits.snapshot()
    audit_reservations = await validator_audits.reservation_health()
    return {
        "quorum": {
            "pending": quorum_counts.get("pending", 0),
            "accepted": quorum_counts.get("accepted", 0),
            "disputed": quorum_counts.get("disputed", 0),
            "finalized": quorum_counts.get("finalized", 0),
        },
        "quorum_policy": {
            "threshold": QUORUM_MIN,
            "target_validators": QUORUM_TARGET,
            "distinct_registered_validators": True,
            "operator_independence_proven": network_health["operator_independence"]["proven"],
            "operator_independence_required_for_acceptance": False,
        },
        "stages": {
            "probes_completed": probe_counts.get("completed", 0),
            "authoritative_evidence_accepted": authoritative_evidence,
            "workers_passed": worker_passed,
            "quorum_reached": quorum_reached,
            "groups_finalized": quorum_counts.get("finalized", 0),
        },
        "validators": {
            "active": active_validators,
            "heartbeat_fresh": fresh_validators,
            "participating_24h": participating_validators,
            "heartbeat_fresh_seconds": VALIDATOR_HEARTBEAT_FRESH_SECONDS,
        },
        "network": network_health,
        "paid_audit": {
            "policy": audit_policy,
            "budget": audit_budget,
            "reservations": audit_reservations,
        },
        "probe": probe_counts,
        "recent": recent,
        "economic_effect": "none",
    }


def _normalize(payload: dict[str, Any], signature: str | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AttestationError("payload must be an object")
    if _payload_size(payload) > MAX_PAYLOAD_BYTES:
        raise AttestationError("payload is too large")

    verdict = _string(payload, "verdict", 16)
    if verdict not in VALID_VERDICTS:
        raise AttestationError("payload.verdict must be healthy, slow, or failed")

    sig = _normalize_signature(signature)
    validator_wallet = _validator_wallet(payload)
    assignment_source = _string(payload, "assignment_source", 32)
    wants_authority = assignment_source == "grid" or bool(_string(payload, "grid_nonce", 128))
    if assignment_source == "grid" and not _string(payload, "assignment_id", 96):
        raise AttestationError("grid assignment evidence requires payload.assignment_id")
    if wants_authority and not _string(payload, "grid_nonce", 128):
        raise AttestationError("authoritative evidence requires payload.grid_nonce")
    if wants_authority and not _string(payload, "evidence_hash", 64):
        raise AttestationError("authoritative evidence requires payload.evidence_hash")
    if wants_authority and not _string(payload, "probe_group_id", 96):
        raise AttestationError("authoritative evidence requires payload.probe_group_id")
    signature_status = _signature_status(payload, sig)
    if wants_authority and signature_status != "verified":
        raise AttestationError("authoritative evidence requires a verified validator signature")

    return {
        "attestation_hash": _attestation_hash(payload, sig),
        "validator_wallet": validator_wallet,
        "assignment_id": _string(payload, "assignment_id", 96) if wants_authority else None,
        "probe_group_id": _string(payload, "probe_group_id", 96) if wants_authority else None,
        "grid_nonce": _string(payload, "grid_nonce", 128) if wants_authority else None,
        "evidence_hash": _string(payload, "evidence_hash", 64) if wants_authority else None,
        "authority": "authoritative" if wants_authority else "preview",
        "quorum_status": "pending",
        "worker_id": _string(payload, "worker_id", 64) or _string(payload, "worker", 64),
        "model": _string(payload, "model", 255),
        "modality": _string(payload, "modality", 16),
        "capability": _string(payload, "capability", 128),
        "canary_kind": _string(payload, "canary_kind", 64) or _string(payload, "challenge_type", 64),
        "nonce": _string(payload, "nonce", 128),
        "verdict": verdict,
        "score": _float(payload, "score"),
        "latency_ms": _int(payload, "latency_ms"),
        "epoch": _string(payload, "epoch", 64),
        "signature": sig,
        "signature_status": signature_status,
        "payload": payload,
    }


async def _verify_assignment_in_session(
    session,
    *,
    account_id,
    validator_id: str,
    row: dict[str, Any],
) -> dict[str, Any]:
    assignment_id = row.get("assignment_id")
    grid_nonce = row.get("grid_nonce")
    assignment = (
        await session.execute(
            sa.select(assignments_t).where(assignments_t.c.id == assignment_id)
        )
    ).mappings().first()
    if not assignment:
        raise AttestationError("assignment_id is not a Grid-issued assignment")
    if assignment["account_id"] != account_id:
        raise AttestationError("assignment does not belong to this validator account")
    if assignment["validator_id"] != validator_id:
        raise AttestationError("assignment does not belong to this registered validator")
    validator = (
        await session.execute(
            sa.select(validators_t).where(
                validators_t.c.id == validator_id,
                validators_t.c.account_id == account_id,
                validators_t.c.status == "active",
            ),
        )
    ).mappings().first()
    if not validator:
        raise AttestationError("active validator registration required")
    if row.get("validator_wallet") != validator["signing_wallet"]:
        raise AttestationError("attestation wallet does not match validator registration")
    if assignment["grid_nonce"] != grid_nonce:
        raise AttestationError("grid_nonce does not match assignment")
    if not assignment["probe_group_id"]:
        raise AttestationError("assignment is not part of a shared probe group")
    if assignment["probe_group_id"] != row.get("probe_group_id"):
        raise AttestationError("probe_group_id does not match assignment")
    if assignment["expires"]:
        attestation_deadline = _aware(assignment["expires"]) + timedelta(
            seconds=ATTESTATION_GRACE_SECONDS
        )
        if attestation_deadline < _now():
            raise AttestationError("assignment attestation window has expired")
    if assignment["probe_status"] != "completed":
        raise AttestationError("assignment probe has not completed")
    if not assignment["probe_evidence_hash"]:
        raise AttestationError("assignment is missing probe evidence")
    if not assignment["probe_verdict"]:
        raise AttestationError("assignment is missing probe verdict")
    if row.get("evidence_hash") != assignment["probe_evidence_hash"]:
        raise AttestationError("payload.evidence_hash does not match assignment probe")
    checks = {
        "worker_id": assignment["target_worker_id"],
        "model": assignment["model"],
        "modality": assignment["modality"],
        "capability": assignment["capability"],
        "canary_kind": assignment["canary_kind"],
    }
    for key, expected in checks.items():
        if row.get(key) and row[key] != expected:
            raise AttestationError(f"payload.{key} does not match assignment")
        row[key] = expected
    row["score"] = VERDICT_SCORE[row["verdict"]]
    return assignment


async def _update_quorum_in_session(session, probe_group_id: str) -> str:
    group = (
        await session.execute(
            sa.select(probe_groups_t).where(probe_groups_t.c.id == probe_group_id)
        )
    ).mappings().first()
    if not group:
        raise AttestationError("probe group is missing")
    if group["quorum_status"] == "finalized":
        raise AttestationError("probe group is finalized")
    if group["expires"]:
        attestation_deadline = _aware(group["expires"]) + timedelta(
            seconds=ATTESTATION_GRACE_SECONDS
        )
        if attestation_deadline < _now():
            raise AttestationError("probe group attestation window has expired")
    rows = (
        await session.execute(
            sa.select(attestations_t.c.verdict, sa.func.count().label("count"))
            .where(
                attestations_t.c.probe_group_id == probe_group_id,
                attestations_t.c.authority == "authoritative",
                attestations_t.c.validator_id.isnot(None),
            )
            .group_by(attestations_t.c.verdict)
        )
    ).mappings().all()
    counts = {row["verdict"]: int(row["count"]) for row in rows}
    threshold = int(group["quorum_threshold"] or QUORUM_MIN)
    winners = [verdict for verdict, count in counts.items() if count >= threshold]
    now = _now()
    if not counts:
        status = "pending"
        outcome = None
    elif len(counts) > 1:
        status = "disputed"
        outcome = winners[0] if len(winners) == 1 else "disputed"
    elif len(winners) == 1:
        status = "accepted"
        outcome = winners[0]
    else:
        status = "pending"
        outcome = None
    await session.execute(
        sa.update(probe_groups_t)
        .where(probe_groups_t.c.id == probe_group_id)
        .values(
            status=status,
            quorum_status=status,
            quorum_outcome=outcome,
            accepted=now if status == "accepted" and not group["accepted"] else group["accepted"],
            disputed=now if status == "disputed" and not group["disputed"] else group["disputed"],
        )
    )
    await session.execute(
        sa.update(assignments_t)
        .where(assignments_t.c.probe_group_id == probe_group_id)
        .values(status=status, quorum_status=status, quorum_outcome=outcome)
    )
    await session.execute(
        sa.update(attestations_t)
        .where(attestations_t.c.probe_group_id == probe_group_id)
        .values(quorum_status=status)
    )
    return status


async def record_attestation(
    *,
    account_id,
    validator_id: str | None = None,
    payload: dict[str, Any],
    signature: str | None = None,
) -> dict[str, Any]:
    """Store one validator attestation idempotently.

    Preview attestations are preserved for rollout/debugging. Authoritative
    attestations require a verified Grid assignment id + nonce and update the
    assignment quorum state. No route/reward/slash side effects happen here.
    """
    row = _normalize(payload, signature)
    row["account_id"] = account_id
    row["created"] = _now()

    async with await new_session() as session:
        if row["authority"] == "authoritative":
            if not validator_id:
                raise AttestationError("active validator registration required")
            await _verify_assignment_in_session(
                session,
                account_id=account_id,
                validator_id=validator_id,
                row=row,
            )
            row["validator_id"] = validator_id
            # Lock before inserting the FK-bound vote. Locking after the insert
            # can deadlock when concurrent transactions upgrade their implicit
            # FK key-share locks to row-update locks.
            locked_group = (
                await session.execute(
                    sa.select(
                        probe_groups_t.c.id,
                        probe_groups_t.c.quorum_status,
                        probe_groups_t.c.expires,
                    )
                    .where(probe_groups_t.c.id == row["probe_group_id"])
                    .with_for_update()
                )
            ).first()
            if not locked_group:
                raise AttestationError("probe group is missing")
            if locked_group[1] == "finalized":
                raise AttestationError("probe group is finalized")
            if locked_group[2]:
                attestation_deadline = _aware(locked_group[2]) + timedelta(
                    seconds=ATTESTATION_GRACE_SECONDS
                )
                if attestation_deadline < _now():
                    raise AttestationError("probe group attestation window has expired")
            existing_for_validator = (
                await session.execute(
                    sa.select(
                        attestations_t.c.id,
                        attestations_t.c.attestation_hash,
                        attestations_t.c.quorum_status,
                    ).where(
                        attestations_t.c.probe_group_id == row["probe_group_id"],
                        attestations_t.c.validator_id == validator_id,
                    ),
                )
            ).first()
            if existing_for_validator:
                if existing_for_validator[1] != row["attestation_hash"]:
                    raise AttestationError(
                        "validator already submitted an authoritative "
                        "attestation for this probe group"
                    )
                return {
                    "status": "duplicate",
                    "id": existing_for_validator[0],
                    "attestation_hash": row["attestation_hash"],
                    "signature_status": row["signature_status"],
                    "authority": row["authority"],
                    "assignment_id": row["assignment_id"],
                    "probe_group_id": row["probe_group_id"],
                    "quorum_status": existing_for_validator[2],
                }
        try:
            result = await session.execute(sa.insert(attestations_t).values(**row))
            attestation_id = result.inserted_primary_key[0] if result.inserted_primary_key else None
            status = "accepted"
        except IntegrityError:
            await session.rollback()
            existing = (
                await session.execute(
                    sa.select(
                        attestations_t.c.id,
                        attestations_t.c.assignment_id,
                        attestations_t.c.authority,
                    ).where(attestations_t.c.attestation_hash == row["attestation_hash"])
                )
            ).first()
            if not existing and row.get("probe_group_id") and row.get("validator_id"):
                conflicting = (
                    await session.execute(
                        sa.select(attestations_t.c.id).where(
                            attestations_t.c.probe_group_id == row["probe_group_id"],
                            attestations_t.c.validator_id == row["validator_id"],
                        ),
                    )
                ).first()
                if conflicting:
                    raise AttestationError(
                        "validator already submitted an authoritative "
                        "attestation for this probe group"
                    )
            attestation_id = existing[0] if existing else None
            row["assignment_id"] = existing[1] if existing else row.get("assignment_id")
            row["authority"] = existing[2] if existing else row.get("authority")
            status = "duplicate"
        quorum_status = "preview"
        if row["authority"] == "authoritative" and row.get("probe_group_id"):
            quorum_status = await _update_quorum_in_session(session, row["probe_group_id"])
        await session.commit()

    logger.info(
        "validator attestation %s account=%s authority=%s verdict=%s model=%s assignment=%s",
        status,
        opaque_id(account_id),
        row["authority"],
        row["verdict"],
        row["model"] or "-",
        opaque_id(row.get("assignment_id")),
    )
    return {
        "status": status,
        "id": attestation_id,
        "attestation_hash": row["attestation_hash"],
        "signature_status": row["signature_status"],
        "authority": row["authority"],
        "assignment_id": row.get("assignment_id"),
        "probe_group_id": row.get("probe_group_id"),
        "quorum_status": quorum_status,
    }


async def scorecards(
    *,
    limit: int = 100,
    since_hours: int = 168,
    worker_id: str | None = None,
    model: str | None = None,
    authority: str = "all",
) -> dict[str, Any]:
    """Return aggregate validator evidence without economic side effects."""
    safe_limit = max(1, min(int(limit), 500))
    safe_since = max(1, min(int(since_hours), 24 * 90))
    mode = authority if authority in VALID_AUTHORITY else "all"
    cutoff = _now() - timedelta(hours=safe_since)

    healthy = sa.func.sum(sa.case((attestations_t.c.verdict == "healthy", 1), else_=0))
    slow = sa.func.sum(sa.case((attestations_t.c.verdict == "slow", 1), else_=0))
    failed = sa.func.sum(sa.case((attestations_t.c.verdict == "failed", 1), else_=0))
    objective = assignments_t.c.probe_verdict.in_(tuple(sorted(VALID_VERDICTS)))
    core_matched = sa.func.sum(
        sa.case(
            (
                sa.and_(
                    objective,
                    attestations_t.c.verdict == assignments_t.c.probe_verdict,
                ),
                1,
            ),
            else_=0,
        )
    )
    core_disagreed = sa.func.sum(
        sa.case(
            (
                sa.and_(
                    objective,
                    attestations_t.c.verdict != assignments_t.c.probe_verdict,
                ),
                1,
            ),
            else_=0,
        )
    )

    q = (
        sa.select(
            attestations_t.c.authority,
            attestations_t.c.quorum_status,
            attestations_t.c.worker_id,
            attestations_t.c.model,
            attestations_t.c.modality,
            attestations_t.c.capability,
            sa.func.count().label("total"),
            healthy.label("healthy"),
            slow.label("slow"),
            failed.label("failed"),
            core_matched.label("core_matched"),
            core_disagreed.label("core_disagreed"),
            sa.func.avg(attestations_t.c.latency_ms).label("avg_latency_ms"),
            sa.func.avg(attestations_t.c.score).label("avg_score"),
            sa.func.min(attestations_t.c.created).label("first_seen"),
            sa.func.max(attestations_t.c.created).label("last_seen"),
        )
        .outerjoin(
            assignments_t,
            assignments_t.c.id == attestations_t.c.assignment_id,
        )
        .where(attestations_t.c.created >= cutoff)
        .group_by(
            attestations_t.c.authority,
            attestations_t.c.quorum_status,
            attestations_t.c.worker_id,
            attestations_t.c.model,
            attestations_t.c.modality,
            attestations_t.c.capability,
        )
        .order_by(sa.func.max(attestations_t.c.created).desc())
        .limit(safe_limit)
    )
    if mode != "all":
        q = q.where(attestations_t.c.authority == mode)
    if worker_id:
        q = q.where(attestations_t.c.worker_id == worker_id)
    if model:
        q = q.where(attestations_t.c.model == model)

    async with await new_session() as session:
        rows = (await session.execute(q)).mappings().all()

    items = []
    for row in rows:
        total = int(row["total"] or 0)
        failures = int(row["failed"] or 0)
        healthy_count = int(row["healthy"] or 0)
        slow_count = int(row["slow"] or 0)
        matched_count = int(row["core_matched"] or 0)
        disagreed_count = int(row["core_disagreed"] or 0)
        opinion_count = max(0, total - matched_count - disagreed_count)
        if matched_count + disagreed_count == total and total:
            verdict_basis = "objective_core_cross_check"
        elif opinion_count == total:
            verdict_basis = "validator_opinion"
        else:
            verdict_basis = "mixed"
        subject_type = "worker" if row["worker_id"] else "model"
        subject_id = row["worker_id"] or row["model"] or "unknown"
        items.append({
            "subject_type": subject_type,
            "subject_id": subject_id,
            "worker_id": row["worker_id"],
            "model": row["model"],
            "modality": row["modality"],
            "capability": row["capability"],
            "score_dimension": _score_dimension(row["modality"], row["capability"]),
            "quality_eligible": _quality_eligible(row["modality"], row["capability"]),
            "quality_score": None,
            "verdict_verification": {
                "basis": verdict_basis,
                "core_matched": matched_count,
                "core_disagreed": disagreed_count,
                "validator_opinion": opinion_count,
            },
            "authority": row["authority"],
            "quorum_status": row["quorum_status"],
            "total": total,
            "healthy": healthy_count,
            "slow": slow_count,
            "failed": failures,
            "healthy_rate": (healthy_count / total) if total else 0.0,
            "slow_rate": (slow_count / total) if total else 0.0,
            "failed_rate": (failures / total) if total else 0.0,
            "avg_latency_ms": (
                float(row["avg_latency_ms"]) if row["avg_latency_ms"] is not None else None
            ),
            "avg_score": float(row["avg_score"]) if row["avg_score"] is not None else None,
            "first_seen": row["first_seen"].isoformat() if row["first_seen"] else None,
            "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None,
        })

    return {
        "items": items,
        "count": len(items),
        "window_hours": safe_since,
        "limit": safe_limit,
        "authority": mode,
        "filters": {
            "worker_id": worker_id,
            "model": model,
        },
        "economic_effect": "none",
    }


async def probe_assignment(
    *,
    account_id,
    validator_id: str,
    assignment_id: str,
) -> dict[str, Any]:
    """Run a stored assignment against exactly its target worker.

    This queues a hard-targeted validator probe job and waits for the worker
    response. It never reserves demand credits, rewards validators, strikes, or
    slashes. A text assignment snapshotted as ``audit_budget`` compensates the
    target worker through the bounded network audit ledger. The caller can use
    the returned hashes in a signed attestation.
    """
    row, job_id = await _claim_probe_lease(
        account_id=account_id,
        validator_id=validator_id,
        assignment_id=assignment_id,
    )
    replay_result = row.pop("_replay_result", None)
    if replay_result is not None:
        return replay_result
    if row["modality"] == "image":
        return await _probe_image_assignment(row=row, assignment_id=assignment_id, job_id=job_id)
    if row["modality"] == "video":
        return await _probe_video_assignment(row=row, assignment_id=assignment_id, job_id=job_id)
    challenge = row["challenge"] or {}
    prompt = str(challenge.get("prompt") or "")
    started = _now()
    kind = str(challenge.get("kind") or "")
    tool_chain = None
    request = {
        "model": row["model"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": int(challenge.get("max_tokens") or 32),
        "temperature": float(challenge.get("temperature") or 0),
        "stream": True,
    }
    for key in ("tools", "tool_choice", "stop"):
        if key in challenge:
            request[key] = challenge[key]

    if kind == "tool.chain":
        steps = challenge.get("steps")
        if not isinstance(steps, list) or len(steps) != 2:
            await _mark_probe(job_id, "failed")
            raise AssignmentError("tool-chain assignment is malformed")
        request.update({"tools": steps[0]["tools"], "tool_choice": steps[0]["tool_choice"]})

    stage = await _run_targeted_text_stage(
        row=row,
        assignment_id=assignment_id,
        job_id=job_id,
        prompt=prompt,
        request=request,
    )
    if stage.get("status") != "completed":
        await _mark_probe(job_id, str(stage.get("probe_status") or "failed"))
        return {"assignment_id": assignment_id, "job_id": job_id, **stage}

    full_text = str(stage.get("full_text") or "")
    full_reasoning = str(stage.get("full_reasoning") or "")
    tool_calls = stage.get("tool_calls")
    finish_reason = stage.get("finish_reason")
    usage = stage.get("usage")
    grid_meta = stage.get("grid")

    if kind == "tool.chain":
        first = {
            "text": full_text,
            "tool_calls": tool_calls,
            "finish_reason": finish_reason,
        }
        tool_chain = [first]
        normalized_first = _normalized_tool_call(tool_calls) if not _strip_think(full_text) else None
        first_hash = str(challenge["steps"][0].get("expected_hash") or "")
        first_call = tool_calls[0] if isinstance(tool_calls, list) and len(tool_calls) == 1 else {}
        call_id = first_call.get("id") if isinstance(first_call, dict) else None
        first_valid = bool(
            normalized_first
            and isinstance(call_id, str)
            and call_id.strip()
            and re.fullmatch(r"[0-9a-f]{64}", first_hash)
            and secrets.compare_digest(_hash_text(normalized_first), first_hash)
        )
        if first_valid:
            second_step = challenge["steps"][1]
            second_request = {
                "model": row["model"],
                "messages": [
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": None, "tool_calls": tool_calls},
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": _canonical(second_step["tool_result"]),
                    },
                ],
                "tools": second_step["tools"],
                "tool_choice": second_step["tool_choice"],
                "max_tokens": int(challenge.get("max_tokens") or 32),
                "temperature": float(challenge.get("temperature") or 0),
                "stream": True,
            }
            second_job_id = str(uuid4())
            second = await _run_targeted_text_stage(
                row=row,
                assignment_id=assignment_id,
                job_id=second_job_id,
                prompt=prompt,
                request=second_request,
            )
            if second.get("status") != "completed":
                await _mark_probe(job_id, str(second.get("probe_status") or "failed"))
                return {"assignment_id": assignment_id, "job_id": job_id, **second}
            full_text = str(second.get("full_text") or "")
            full_reasoning = str(second.get("full_reasoning") or "")
            tool_calls = second.get("tool_calls")
            finish_reason = second.get("finish_reason")
            usage = second.get("usage")
            grid_meta = second.get("grid")
            tool_chain.append({
                "text": full_text,
                "tool_calls": tool_calls,
                "finish_reason": finish_reason,
            })

    if kind == "tool.call":
        response_commitment = _canonical({"text": full_text, "tool_calls": tool_calls})
    elif kind == "tool.chain":
        response_commitment = _canonical({"steps": tool_chain})
    elif kind == "token.limit":
        response_commitment = _canonical({
            "text": full_text,
            "reasoning": full_reasoning,
            "finish_reason": finish_reason,
        })
    else:
        response_commitment = full_text
    prompt_commitment = (
        _canonical({"prompt": prompt, "steps": challenge.get("steps")})
        if kind == "tool.chain"
        else prompt
    )
    evidence = {
        "assignment_id": assignment_id,
        "probe_group_id": row["probe_group_id"],
        "grid_nonce": row["grid_nonce"],
        "worker_id": row["target_worker_id"],
        "model": row["model"],
        "modality": row["modality"],
        "capability": row["capability"],
        "canary_kind": row["canary_kind"],
        "prompt_hash": _hash_text(prompt_commitment),
        "response_hash": _hash_text(response_commitment),
    }
    evidence["evidence_hash"] = _hash_obj(evidence)
    latency_ms = int((_now() - started).total_seconds() * 1000)
    probe_verdict = _score_text_challenge(
        row["challenge"] or {},
        full_text,
        latency_ms,
        tool_calls=tool_calls,
        tool_chain=tool_chain,
        reasoning_text=full_reasoning,
        finish_reason=finish_reason,
    )
    result = {
        "status": "completed",
        "assignment_id": assignment_id,
        "job_id": job_id,
        "target_worker_name": row["target_worker_name"],
        **_assignment_disclosure(row),
        "output_text": full_text,
        "reasoning_text": full_reasoning if kind == "token.limit" else None,
        "tool_calls": tool_calls,
        "tool_chain": tool_chain,
        "finish_reason": finish_reason,
        "usage": usage,
        "grid": grid_meta,
        "probe_latency_ms": latency_ms,
        **evidence,
        "economic_effect": (
            "worker_compensated_audit"
            if row.get("worker_compensation") == "audit_budget"
            else "none"
        ),
    }
    if not await _mark_probe(
        job_id,
        "completed",
        prompt_hash=evidence["prompt_hash"],
        response_hash=evidence["response_hash"],
        evidence_hash=evidence["evidence_hash"],
        verdict=probe_verdict,
        latency_ms=latency_ms,
        result=result,
    ):
        raise AssignmentError("completed probe result could not be persisted")
    return result


def _media_response_commitment(witnesses: list[dict[str, Any]]) -> str:
    committed = [
        {
            "role": str(witness["role"]),
            "worker_id": str(witness["worker_id"]),
            "sha256": str(witness["sha256"]).lower(),
            "bytes": int(witness["bytes"]),
            "content_type": str(witness["content_type"]).lower(),
            "latency_ms": int(witness["latency_ms"]),
        }
        for witness in witnesses
    ]
    return _canonical({"witnesses": committed})


def _validated_media_witnesses(
    row: dict[str, Any],
    raw_witnesses: Any,
) -> list[dict[str, Any]]:
    """Normalize and bind a stored witness set to its private challenge."""
    challenge = row["challenge"] or {}
    modality = str(row.get("modality") or "")
    reference_ids = [str(value) for value in (challenge.get("reference_worker_ids") or [])]
    expected = [
        ("candidate", str(row["target_worker_id"])),
        *(("reference", worker_id) for worker_id in reference_ids),
    ]
    expected_count = 3 if modality == "image" else 1 if modality == "video" else 0
    if (
        not isinstance(raw_witnesses, list)
        or len(raw_witnesses) != expected_count
        or len(expected) != expected_count
    ):
        raise AssignmentError(f"{modality or 'media'} probe witness set is malformed")

    if modality not in {"image", "video"}:
        raise AssignmentError("unsupported media probe modality")
    policy = media_validation_policy() if modality == "image" else video_validation_policy()
    max_bytes = int(policy["max_output_bytes"])
    expected_content_type = "image/webp" if modality == "image" else "video/mp4"
    normalized: list[dict[str, Any]] = []
    try:
        for raw, (expected_role, expected_worker_id) in zip(
            raw_witnesses,
            expected,
            strict=True,
        ):
            if not isinstance(raw, dict):
                raise ValueError
            witness = {
                "role": str(raw["role"]),
                "worker_id": str(raw["worker_id"]),
                "url": str(raw["url"]),
                "sha256": str(raw["sha256"]).lower(),
                "bytes": int(raw["bytes"]),
                "content_type": str(raw["content_type"]).lower(),
                "latency_ms": int(raw["latency_ms"]),
            }
            if (
                witness["role"] != expected_role
                or witness["worker_id"] != expected_worker_id
                or not witness["url"].startswith("https://")
                or not _SHA256_RE.fullmatch(witness["sha256"])
                or not 0 < witness["bytes"] <= max_bytes
                or witness["content_type"] != expected_content_type
                or witness["latency_ms"] < 0
            ):
                raise ValueError
            normalized.append(witness)
    except (KeyError, TypeError, ValueError) as exc:
        raise AssignmentError(f"{modality or 'media'} probe witness set is malformed") from exc
    if len({witness["url"] for witness in normalized}) != expected_count:
        raise AssignmentError(f"{modality or 'media'} probe witness URLs are not independent")
    return normalized


def _verified_media_group_witnesses(
    row: dict[str, Any],
    group: dict[str, Any],
) -> list[dict[str, Any]]:
    witnesses = _validated_media_witnesses(row, group.get("probe_witnesses"))
    stored_hash = str(group.get("probe_witness_hash") or "")
    if not _SHA256_RE.fullmatch(stored_hash) or not secrets.compare_digest(
        stored_hash,
        _hash_obj({"witnesses": witnesses}),
    ):
        raise AssignmentError("media probe witness commitment is invalid")
    return witnesses


def _verify_media_group_binding(row: dict[str, Any], group: dict[str, Any]) -> None:
    """Prove an assignment still names the immutable challenge it joined."""
    group_id = str(row.get("probe_group_id") or "")
    challenge = row.get("challenge") or {}
    expected_hash = _hash_obj({
        "group_id": group_id,
        "worker_id": str(row["target_worker_id"]),
        "model": str(row["model"]),
        "challenge": challenge,
    })
    bound = (
        str(group.get("id") or "") == group_id
        and str(group.get("target_worker_id") or "") == str(row["target_worker_id"])
        and str(group.get("model") or "") == str(row["model"])
        and group.get("modality") == row.get("modality")
        and group.get("capability") == row["capability"]
        and group.get("canary_kind") == row["canary_kind"]
        and _canonical(group.get("challenge") or {}) == _canonical(challenge)
        and secrets.compare_digest(str(group.get("challenge_hash") or ""), expected_hash)
    )
    if not bound:
        raise AssignmentError("media assignment does not match its probe group")


async def _claim_media_group_execution(
    *,
    group_id: str,
    owner_job_id: str,
    modality: str,
) -> tuple[str, dict[str, Any]]:
    """Claim the one GPU execution for a media group or observe its state."""
    if modality not in {"image", "video"}:
        raise AssignmentError("unsupported media probe modality")
    now = _now()
    policy = media_validation_policy() if modality == "image" else video_validation_policy()
    lease_seconds = max(PROBE_LEASE_SECONDS, int(policy["probe_timeout_seconds"]) + 120)
    retryable = probe_groups_t.c.probe_status.in_(("not_started", "failed", "timeout"))
    stale_running = sa.and_(
        probe_groups_t.c.probe_status == "running",
        sa.or_(
            probe_groups_t.c.probe_lease_expires.is_(None),
            probe_groups_t.c.probe_lease_expires <= now,
        ),
    )
    async with await new_session() as session:
        claimed = await session.execute(
            sa.update(probe_groups_t)
            .where(
                probe_groups_t.c.id == group_id,
                probe_groups_t.c.modality == modality,
                probe_groups_t.c.expires >= now,
                probe_groups_t.c.probe_attempts < PROBE_MAX_ATTEMPTS,
                sa.or_(retryable, stale_running),
            )
            .values(
                probe_job_id=owner_job_id,
                probe_status="running",
                probe_attempts=probe_groups_t.c.probe_attempts + 1,
                probe_lease_expires=now + timedelta(seconds=lease_seconds),
            )
        )
        await session.commit()
        group = (
            await session.execute(
                sa.select(probe_groups_t).where(probe_groups_t.c.id == group_id)
            )
        ).mappings().first()
    if not group:
        raise AssignmentError("media probe group not found")
    group = dict(group)
    if claimed.rowcount == 1:
        return "claimed", group
    if group["probe_status"] == "completed":
        return "completed", group
    if group["probe_status"] == "running":
        return "running", group
    if int(group["probe_attempts"] or 0) >= PROBE_MAX_ATTEMPTS:
        return "exhausted", group
    return "retryable", group


async def _complete_media_group(
    *,
    group_id: str,
    owner_job_id: str,
    witnesses: list[dict[str, Any]],
) -> dict[str, Any]:
    witness_hash = _hash_obj({"witnesses": witnesses})
    now = _now()
    async with await new_session() as session:
        completed = await session.execute(
            sa.update(probe_groups_t)
            .where(
                probe_groups_t.c.id == group_id,
                probe_groups_t.c.probe_job_id == owner_job_id,
                probe_groups_t.c.probe_status == "running",
            )
            .values(
                probe_status="completed",
                probe_lease_expires=None,
                probe_witnesses=witnesses,
                probe_witness_hash=witness_hash,
                probe_completed=now,
            )
        )
        await session.commit()
        group = (
            await session.execute(
                sa.select(probe_groups_t).where(probe_groups_t.c.id == group_id)
            )
        ).mappings().first()
    if not group:
        raise AssignmentError("media probe group not found")
    group = dict(group)
    if completed.rowcount != 1 and (
        group["probe_status"] != "completed"
        or not secrets.compare_digest(str(group["probe_witness_hash"] or ""), witness_hash)
    ):
        raise AssignmentError("media probe group lease was lost")
    return group


async def _fail_media_group(*, group_id: str, owner_job_id: str, status: str = "failed") -> None:
    try:
        async with await new_session() as session:
            await session.execute(
                sa.update(probe_groups_t)
                .where(
                    probe_groups_t.c.id == group_id,
                    probe_groups_t.c.probe_job_id == owner_job_id,
                    probe_groups_t.c.probe_status == "running",
                )
                .values(probe_status=status, probe_lease_expires=None)
            )
            await session.commit()
    except Exception as exc:
        logger.warning(
            "failed to mark media probe group=%s status=%s error_type=%s",
            opaque_id(group_id),
            status,
            error_type(exc),
        )


async def _probe_image_assignment(
    *,
    row: dict[str, Any],
    assignment_id: str,
    job_id: str,
) -> dict[str, Any]:
    """Execute one group witness set or reuse it for independent scoring."""
    from . import recipes

    challenge = row["challenge"] or {}
    reference_ids = [str(value) for value in (challenge.get("reference_worker_ids") or [])]
    if len(reference_ids) != 2 or len({str(row["target_worker_id"]), *reference_ids}) != 3:
        await _mark_probe(job_id, "failed")
        raise AssignmentError("image assignment reference set is malformed")
    recipe = recipes.get_recipe(str(challenge.get("recipe_root") or ""))
    if (
        recipe is None
        or not recipe.deterministic
        or recipe.recipe_id != challenge.get("recipe_id")
        or recipe.model_name != row["model"]
        or recipe.model_digest != challenge.get("model_digest")
    ):
        await _mark_probe(job_id, "failed")
        raise AssignmentError("image assignment recipe is no longer authoritative")

    reference_uuid_values = []
    try:
        from uuid import UUID

        reference_uuid_values = [UUID(value) for value in reference_ids]
    except ValueError as exc:
        await _mark_probe(job_id, "failed")
        raise AssignmentError("image assignment reference identity is invalid") from exc
    async with await new_session() as session:
        reference_rows = (
            await session.execute(
                sa.select(workers_t.c.id, workers_t.c.name).where(
                    workers_t.c.id.in_(reference_uuid_values)
                )
            )
        ).all()
    names = {str(worker_id): str(name) for worker_id, name in reference_rows}
    if set(names) != set(reference_ids):
        await _mark_probe(job_id, "failed")
        raise AssignmentError("image assignment reference worker is unavailable")

    parameters = challenge.get("parameters") or {}
    inputs = {
        "prompt": challenge.get("prompt"),
        "seed": challenge.get("seed"),
        "width": parameters.get("width"),
        "height": parameters.get("height"),
    }
    for challenge_name, recipe_name in (
        ("steps", "steps"),
        ("cfg_scale", "cfg"),
        ("sampler", "sampler"),
        ("scheduler", "scheduler"),
    ):
        if challenge_name in parameters:
            inputs[recipe_name] = parameters[challenge_name]
    try:
        resolved = recipes.resolve(recipe.recipe_root, inputs)
    except recipes.RecipeError as exc:
        await _mark_probe(job_id, "failed")
        raise AssignmentError("image assignment recipe inputs are invalid") from exc

    group_id = str(row["probe_group_id"] or "")
    if not group_id:
        await _mark_probe(job_id, "failed")
        raise AssignmentError("image assignment has no probe group")

    group_deadline = _aware(row["probe_lease_expires"])
    group: dict[str, Any] | None = None
    while _now() < group_deadline:
        state, observed = await _claim_media_group_execution(
            group_id=group_id,
            owner_job_id=job_id,
            modality="image",
        )
        try:
            _verify_media_group_binding(row, observed)
        except AssignmentError:
            if state == "claimed":
                await _fail_media_group(group_id=group_id, owner_job_id=job_id)
            await _mark_probe(job_id, "failed")
            raise
        if state == "completed":
            group = observed
            break
        if state == "claimed":
            stages = [
                ("candidate", str(row["target_worker_id"]), row["target_worker_name"], job_id),
                ("reference", reference_ids[0], names[reference_ids[0]], str(uuid4())),
                ("reference", reference_ids[1], names[reference_ids[1]], str(uuid4())),
            ]
            try:
                results = await asyncio.gather(
                    *(
                        _run_targeted_image_stage(
                            row=row,
                            assignment_id=assignment_id,
                            job_id=stage_job_id,
                            role=role,
                            worker_id=worker_id,
                            worker_name=worker_name,
                            challenge=challenge,
                            resolved=resolved,
                        )
                        for role, worker_id, worker_name, stage_job_id in stages
                    )
                )
            except Exception as exc:
                logger.error(
                    "validator image group execution failed group=%s error_type=%s",
                    opaque_id(group_id),
                    error_type(exc),
                )
                await _fail_media_group(group_id=group_id, owner_job_id=job_id)
                await _mark_probe(job_id, "failed")
                return {
                    "status": "error",
                    "probe_status": "failed",
                    "assignment_id": assignment_id,
                    "job_id": job_id,
                    "message": "image probe execution failed",
                    "code": 502,
                    "economic_effect": "none",
                }
            if any(result.get("status") != "completed" for result in results):
                await _fail_media_group(group_id=group_id, owner_job_id=job_id)
                await _mark_probe(job_id, "failed")
                return {
                    "status": "error",
                    "probe_status": "failed",
                    "assignment_id": assignment_id,
                    "job_id": job_id,
                    "message": "image probe was inconclusive",
                    "code": 502,
                    "economic_effect": "none",
                }
            try:
                fresh_witnesses = _validated_media_witnesses(
                    row,
                    [result["witness"] for result in results],
                )
                group = await _complete_media_group(
                    group_id=group_id,
                    owner_job_id=job_id,
                    witnesses=fresh_witnesses,
                )
            except AssignmentError:
                await _fail_media_group(group_id=group_id, owner_job_id=job_id)
                await _mark_probe(job_id, "failed")
                raise
            except Exception as exc:
                logger.error(
                    "validator image witness commit failed group=%s error_type=%s",
                    opaque_id(group_id),
                    error_type(exc),
                )
                await _fail_media_group(group_id=group_id, owner_job_id=job_id)
                await _mark_probe(job_id, "failed")
                return {
                    "status": "error",
                    "probe_status": "failed",
                    "assignment_id": assignment_id,
                    "job_id": job_id,
                    "message": "image probe witness commit failed",
                    "code": 503,
                    "economic_effect": "none",
                }
            break
        if state == "exhausted":
            await _mark_probe(job_id, "failed")
            raise AssignmentError("image probe group retry limit reached")
        await asyncio.sleep(0.25)

    if group is None:
        await _mark_probe(job_id, "timeout")
        return {
            "status": "error",
            "probe_status": "timeout",
            "assignment_id": assignment_id,
            "job_id": job_id,
            "message": "image probe group timed out",
            "code": 504,
            "economic_effect": "none",
        }
    try:
        witnesses = _verified_media_group_witnesses(row, group)
    except AssignmentError:
        await _mark_probe(job_id, "failed")
        raise
    response_commitment = _media_response_commitment(witnesses)
    prompt_commitment = _canonical(challenge)
    evidence = {
        "assignment_id": assignment_id,
        "probe_group_id": row["probe_group_id"],
        "grid_nonce": row["grid_nonce"],
        "worker_id": row["target_worker_id"],
        "model": row["model"],
        "modality": row["modality"],
        "capability": row["capability"],
        "canary_kind": row["canary_kind"],
        "prompt_hash": _hash_text(prompt_commitment),
        "response_hash": _hash_text(response_commitment),
    }
    evidence["evidence_hash"] = _hash_obj(evidence)
    candidate_latency = int(witnesses[0]["latency_ms"])
    result = {
        "status": "completed",
        "assignment_id": assignment_id,
        "job_id": job_id,
        "target_worker_name": row["target_worker_name"],
        **_assignment_disclosure(row),
        "witnesses": witnesses,
        "probe_latency_ms": candidate_latency,
        **evidence,
        "economic_effect": "none",
    }
    if not await _mark_probe(
        job_id,
        "completed",
        prompt_hash=evidence["prompt_hash"],
        response_hash=evidence["response_hash"],
        evidence_hash=evidence["evidence_hash"],
        verdict="witnessed",
        latency_ms=candidate_latency,
        result=result,
    ):
        raise AssignmentError("completed image probe result could not be persisted")
    return result


async def _probe_video_assignment(
    *,
    row: dict[str, Any],
    assignment_id: str,
    job_id: str,
) -> dict[str, Any]:
    """Execute one objective video-contract witness or reuse the group result."""
    from . import recipes

    challenge = row["challenge"] or {}
    if challenge.get("reference_worker_ids") not in (None, []):
        await _mark_probe(job_id, "failed")
        raise AssignmentError("video contract assignment cannot name reference workers")
    recipe = recipes.get_recipe(str(challenge.get("recipe_root") or ""))
    if (
        recipe is None
        or recipe.job_type != "video"
        or recipe.recipe_id != challenge.get("recipe_id")
        or recipe.model_name != row["model"]
        or not {"prompt", "seed", "width", "height", "seconds", "fps"}.issubset(
            recipe.vars
        )
        or "image" in recipe.vars
    ):
        await _mark_probe(job_id, "failed")
        raise AssignmentError("video assignment recipe is no longer authoritative")

    parameters = challenge.get("parameters") or {}
    inputs = {
        "prompt": challenge.get("prompt"),
        "seed": challenge.get("seed"),
        "width": parameters.get("width"),
        "height": parameters.get("height"),
        "seconds": parameters.get("duration_s"),
        "fps": parameters.get("fps"),
    }
    for challenge_name, recipe_name in (
        ("steps", "steps"),
        ("cfg_scale", "cfg"),
        ("sampler", "sampler"),
        ("scheduler", "scheduler"),
    ):
        if challenge_name in parameters:
            inputs[recipe_name] = parameters[challenge_name]
    try:
        resolved = recipes.resolve(recipe.recipe_root, inputs)
    except recipes.RecipeError as exc:
        await _mark_probe(job_id, "failed")
        raise AssignmentError("video assignment recipe inputs are invalid") from exc

    group_id = str(row["probe_group_id"] or "")
    if not group_id:
        await _mark_probe(job_id, "failed")
        raise AssignmentError("video assignment has no probe group")

    group_deadline = _aware(row["probe_lease_expires"])
    group: dict[str, Any] | None = None
    while _now() < group_deadline:
        state, observed = await _claim_media_group_execution(
            group_id=group_id,
            owner_job_id=job_id,
            modality="video",
        )
        try:
            _verify_media_group_binding(row, observed)
        except AssignmentError:
            if state == "claimed":
                await _fail_media_group(group_id=group_id, owner_job_id=job_id)
            await _mark_probe(job_id, "failed")
            raise
        if state == "completed":
            group = observed
            break
        if state == "claimed":
            try:
                result = await _run_targeted_video_stage(
                    row=row,
                    assignment_id=assignment_id,
                    job_id=job_id,
                    worker_id=str(row["target_worker_id"]),
                    worker_name=str(row["target_worker_name"]),
                    challenge=challenge,
                    resolved=resolved,
                )
            except Exception as exc:
                logger.error(
                    "validator video group execution failed group=%s error_type=%s",
                    opaque_id(group_id),
                    error_type(exc),
                )
                await _fail_media_group(group_id=group_id, owner_job_id=job_id)
                await _mark_probe(job_id, "failed")
                return {
                    "status": "error",
                    "probe_status": "failed",
                    "assignment_id": assignment_id,
                    "job_id": job_id,
                    "message": "video probe execution failed",
                    "code": 502,
                    "economic_effect": "none",
                }
            if result.get("status") != "completed":
                await _fail_media_group(group_id=group_id, owner_job_id=job_id)
                await _mark_probe(job_id, "failed")
                return {
                    "status": "error",
                    "probe_status": "failed",
                    "assignment_id": assignment_id,
                    "job_id": job_id,
                    "message": "video probe was inconclusive",
                    "code": int(result.get("code") or 502),
                    "economic_effect": "none",
                }
            try:
                fresh_witnesses = _validated_media_witnesses(row, [result["witness"]])
                group = await _complete_media_group(
                    group_id=group_id,
                    owner_job_id=job_id,
                    witnesses=fresh_witnesses,
                )
            except AssignmentError:
                await _fail_media_group(group_id=group_id, owner_job_id=job_id)
                await _mark_probe(job_id, "failed")
                raise
            except Exception as exc:
                logger.error(
                    "validator video witness commit failed group=%s error_type=%s",
                    opaque_id(group_id),
                    error_type(exc),
                )
                await _fail_media_group(group_id=group_id, owner_job_id=job_id)
                await _mark_probe(job_id, "failed")
                return {
                    "status": "error",
                    "probe_status": "failed",
                    "assignment_id": assignment_id,
                    "job_id": job_id,
                    "message": "video probe witness commit failed",
                    "code": 503,
                    "economic_effect": "none",
                }
            break
        if state == "exhausted":
            await _mark_probe(job_id, "failed")
            raise AssignmentError("video probe group retry limit reached")
        await asyncio.sleep(0.25)

    if group is None:
        await _mark_probe(job_id, "timeout")
        return {
            "status": "error",
            "probe_status": "timeout",
            "assignment_id": assignment_id,
            "job_id": job_id,
            "message": "video probe group timed out",
            "code": 504,
            "economic_effect": "none",
        }
    try:
        witnesses = _verified_media_group_witnesses(row, group)
    except AssignmentError:
        await _mark_probe(job_id, "failed")
        raise
    response_commitment = _media_response_commitment(witnesses)
    prompt_commitment = _canonical(challenge)
    evidence = {
        "assignment_id": assignment_id,
        "probe_group_id": row["probe_group_id"],
        "grid_nonce": row["grid_nonce"],
        "worker_id": row["target_worker_id"],
        "model": row["model"],
        "modality": row["modality"],
        "capability": row["capability"],
        "canary_kind": row["canary_kind"],
        "prompt_hash": _hash_text(prompt_commitment),
        "response_hash": _hash_text(response_commitment),
    }
    evidence["evidence_hash"] = _hash_obj(evidence)
    candidate_latency = int(witnesses[0]["latency_ms"])
    result = {
        "status": "completed",
        "assignment_id": assignment_id,
        "job_id": job_id,
        "target_worker_name": row["target_worker_name"],
        **_assignment_disclosure(row),
        "witnesses": witnesses,
        "probe_latency_ms": candidate_latency,
        **evidence,
        "economic_effect": "none",
    }
    if not await _mark_probe(
        job_id,
        "completed",
        prompt_hash=evidence["prompt_hash"],
        response_hash=evidence["response_hash"],
        evidence_hash=evidence["evidence_hash"],
        verdict="witnessed",
        latency_ms=candidate_latency,
        result=result,
    ):
        raise AssignmentError("completed video probe result could not be persisted")
    return result


async def _run_targeted_image_stage(
    *,
    row: dict[str, Any],
    assignment_id: str,
    job_id: str,
    role: str,
    worker_id: str,
    worker_name: str,
    challenge: dict[str, Any],
    resolved: dict[str, Any],
) -> dict[str, Any]:
    from . import job_queue, token_stream

    parameters = challenge["parameters"]
    payload = {
        "prompt": challenge["prompt"],
        "negative_prompt": "",
        "seed": challenge["seed"],
        "width": parameters["width"],
        "height": parameters["height"],
        "steps": parameters.get("steps"),
        "cfg_scale": parameters.get("cfg_scale"),
        "n": 1,
        "ext": "webp",
        "recipe_engine": resolved["engine"],
        "recipe_spec": resolved["spec"],
        "recipe_root": resolved["recipe_root"],
        "recipe_id": resolved["recipe_id"],
        "deterministic": True,
        "_validator_probe": True,
        "_validator_assignment_id": assignment_id,
        "_validator_probe_group_id": row["probe_group_id"],
        "_validator_grid_nonce": row["grid_nonce"],
        "_validator_role": role,
    }
    route_models = resolved.get("required_models") or [row["model"]]
    try:
        await job_queue.submit_job(
            job_id,
            payload,
            route_models,
            job_type="image",
            preferred_worker=worker_name,
            hard_target_worker=worker_name,
        )
    except Exception as exc:
        logger.error(
            "validator image dispatch failed assignment=%s job=%s error_type=%s",
            opaque_id(assignment_id),
            opaque_id(job_id),
            error_type(exc),
        )
        return {"status": "error", "code": 503}

    try:
        timeout = media_validation_policy()["probe_timeout_seconds"]
        async for event in token_stream.subscribe_tokens(job_id, timeout=timeout):
            if event.get("error"):
                return {"status": "error", "code": event.get("code", 502)}
            if event.get("text") != token_stream.DONE_SENTINEL:
                continue
            try:
                body = json.loads(event.get("full_text") or "{}")
                witness = dict(body["witness"])
                grid = event.get("grid") or {}
                committed = {
                    "role": str(witness["role"]),
                    "worker_id": str(witness["worker_id"]),
                    "url": str(witness["url"]),
                    "sha256": str(witness["sha256"]).lower(),
                    "bytes": int(witness["bytes"]),
                    "content_type": str(witness["content_type"]).lower(),
                    "latency_ms": int(witness["latency_ms"]),
                }
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return {"status": "error", "code": 502}
            if (
                committed["role"] != role
                or committed["worker_id"] != worker_id
                or grid.get("worker_id") != worker_id
                or grid.get("assignment_id") != assignment_id
                or grid.get("grid_nonce") != row["grid_nonce"]
            ):
                return {"status": "error", "code": 502}
            return {"status": "completed", "witness": committed}
        return {"status": "error", "code": 504}
    except Exception as exc:
        logger.error(
            "validator image probe failed assignment=%s job=%s error_type=%s",
            opaque_id(assignment_id),
            opaque_id(job_id),
            error_type(exc),
        )
        return {"status": "error", "code": 502}


async def _run_targeted_video_stage(
    *,
    row: dict[str, Any],
    assignment_id: str,
    job_id: str,
    worker_id: str,
    worker_name: str,
    challenge: dict[str, Any],
    resolved: dict[str, Any],
) -> dict[str, Any]:
    from . import job_queue, token_stream

    parameters = challenge["parameters"]
    seed = int(challenge["seed"])
    payload = {
        "prompt": challenge["prompt"],
        "negative_prompt": "",
        "seed": seed,
        "seeds": [seed],
        "width": int(parameters["width"]),
        "height": int(parameters["height"]),
        "frames": int(parameters["frame_count"]),
        "length": int(parameters["frame_count"]),
        "video_length": int(parameters["frame_count"]),
        "fps": float(parameters["fps"]),
        "steps": parameters.get("steps"),
        "cfg_scale": parameters.get("cfg_scale"),
        "sampler_name": parameters.get("sampler"),
        "n": 1,
        "ext": "mp4",
        "recipe_engine": resolved["engine"],
        "recipe_spec": resolved["spec"],
        "recipe_root": resolved["recipe_root"],
        "recipe_id": resolved["recipe_id"],
        "deterministic": bool(resolved.get("deterministic")),
        "_validator_probe": True,
        "_validator_assignment_id": assignment_id,
        "_validator_probe_group_id": row["probe_group_id"],
        "_validator_grid_nonce": row["grid_nonce"],
        "_validator_role": "candidate",
    }
    route_models = resolved.get("required_models") or [row["model"]]
    try:
        await job_queue.submit_job(
            job_id,
            payload,
            route_models,
            job_type="video",
            preferred_worker=worker_name,
            hard_target_worker=worker_name,
        )
    except Exception as exc:
        logger.error(
            "validator video dispatch failed assignment=%s job=%s error_type=%s",
            opaque_id(assignment_id),
            opaque_id(job_id),
            error_type(exc),
        )
        return {"status": "error", "code": 503}

    try:
        timeout = video_validation_policy()["probe_timeout_seconds"]
        async for event in token_stream.subscribe_tokens(job_id, timeout=timeout):
            if event.get("error"):
                return {"status": "error", "code": event.get("code", 502)}
            if event.get("text") != token_stream.DONE_SENTINEL:
                continue
            try:
                body = json.loads(event.get("full_text") or "{}")
                witness = dict(body["witness"])
                grid = event.get("grid") or {}
                committed = {
                    "role": str(witness["role"]),
                    "worker_id": str(witness["worker_id"]),
                    "url": str(witness["url"]),
                    "sha256": str(witness["sha256"]).lower(),
                    "bytes": int(witness["bytes"]),
                    "content_type": str(witness["content_type"]).lower(),
                    "latency_ms": int(witness["latency_ms"]),
                }
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return {"status": "error", "code": 502}
            if (
                committed["role"] != "candidate"
                or committed["worker_id"] != worker_id
                or grid.get("worker_id") != worker_id
                or grid.get("assignment_id") != assignment_id
                or grid.get("grid_nonce") != row["grid_nonce"]
            ):
                return {"status": "error", "code": 502}
            return {"status": "completed", "witness": committed}
        return {"status": "error", "code": 504}
    except Exception as exc:
        logger.error(
            "validator video probe failed assignment=%s job=%s error_type=%s",
            opaque_id(assignment_id),
            opaque_id(job_id),
            error_type(exc),
        )
        return {"status": "error", "code": 502}


async def _run_targeted_text_stage(
    *,
    row: dict[str, Any],
    assignment_id: str,
    job_id: str,
    prompt: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch and witness one stage, reserving audit den when promised."""
    from . import job_queue, token_stream, validator_audits

    paid_audit = row.get("worker_compensation") == "audit_budget"
    if paid_audit:
        try:
            await validator_audits.reserve(
                job_id=job_id,
                assignment_id=assignment_id,
                probe_group_id=str(row["probe_group_id"]),
                grid_nonce=str(row["grid_nonce"]),
                worker_id=str(row["target_worker_id"]),
                model=str(row["model"]),
                validator_wallet=str(row.get("validator_wallet") or ""),
            )
        except validator_audits.AuditBudgetError as exc:
            return {
                "status": "error",
                "probe_status": "failed",
                "message": str(exc),
                "code": 429 if "exhausted" in str(exc) else 503,
            }

    payload = {
        "request": request,
        "api_format": "openai-chat",
        "prompt": prompt,
        "max_length": int(request.get("max_tokens") or 32),
        "temperature": float(request.get("temperature") or 0),
        "_validator_probe": True,
        "_validator_assignment_id": assignment_id,
        "_validator_probe_group_id": row["probe_group_id"],
        "_validator_grid_nonce": row["grid_nonce"],
        "_validator_paid_audit": paid_audit,
    }
    try:
        await job_queue.submit_job(
            job_id,
            payload,
            [row["model"]],
            job_type="text",
            preferred_worker=row["target_worker_name"],
            hard_target_worker=row["target_worker_name"],
        )
    except Exception as exc:
        if paid_audit:
            await validator_audits.release(job_id)
        logger.error(
            "validator probe dispatch failed assignment=%s job=%s error_type=%s",
            opaque_id(assignment_id),
            opaque_id(job_id),
            error_type(exc),
        )
        return {
            "status": "error",
            "probe_status": "failed",
            "message": "probe dispatch failed",
            "code": 503,
        }

    chunks: list[str] = []
    try:
        async for event in token_stream.subscribe_tokens(job_id, timeout=PROBE_TIMEOUT_SECONDS):
            if event.get("error"):
                return {
                    "status": "error",
                    "probe_status": "failed",
                    "message": event.get("error", "probe failed"),
                    "code": event.get("code", 502),
                }
            if event.get("text") == token_stream.DONE_SENTINEL:
                return {
                    "status": "completed",
                    "full_text": event.get("full_text") or "".join(chunks),
                    "full_reasoning": event.get("full_reasoning") or "",
                    "usage": event.get("usage"),
                    "grid": event.get("grid"),
                    "tool_calls": event.get("tool_calls"),
                    "finish_reason": event.get("finish_reason"),
                }
            chunks.append(token_stream.event_content_text(event))
        return {
            "status": "error",
            "probe_status": "timeout",
            "message": "probe timed out",
            "code": 504,
        }
    except Exception as exc:
        logger.error(
            "validator probe failed assignment=%s job=%s error_type=%s",
            opaque_id(assignment_id),
            opaque_id(job_id),
            error_type(exc),
        )
        return {
            "status": "error",
            "probe_status": "failed",
            "message": "probe failed",
            "code": 502,
        }


async def _claim_probe_lease(
    *,
    account_id,
    validator_id: str,
    assignment_id: str,
) -> tuple[dict[str, Any], str]:
    """Atomically claim one bounded probe attempt for an assignment."""
    now = _now()
    lease_expires = now + timedelta(seconds=PROBE_LEASE_SECONDS)
    media_lease_expires = now + timedelta(
        seconds=max(
            PROBE_LEASE_SECONDS,
            int(get_settings().validator_media_probe_timeout_seconds) + 120,
        ),
    )
    # Worker-visible job IDs use the ordinary opaque UUID shape. Assignment
    # attribution stays in Core; a marker here would let workers special-case
    # validation before producing output.
    job_id = str(uuid4())
    retryable = assignments_t.c.probe_status.in_(("not_started", "failed", "timeout"))
    stale_running = sa.and_(
        assignments_t.c.probe_status == "running",
        sa.or_(
            assignments_t.c.probe_lease_expires.is_(None),
            assignments_t.c.probe_lease_expires <= now,
        ),
    )

    async with await new_session() as session:
        claimed = await session.execute(
            sa.update(assignments_t)
            .where(
                assignments_t.c.id == assignment_id,
                assignments_t.c.account_id == account_id,
                assignments_t.c.validator_id == validator_id,
                assignments_t.c.expires >= now,
                assignments_t.c.probe_attempts < PROBE_MAX_ATTEMPTS,
                sa.or_(retryable, stale_running),
            )
            .values(
                probe_job_id=job_id,
                probe_status="running",
                probe_attempts=assignments_t.c.probe_attempts + 1,
                probe_lease_expires=sa.case(
                    (assignments_t.c.modality.in_(("image", "video")), media_lease_expires),
                    else_=lease_expires,
                ),
                probed=now,
            )
        )
        await session.commit()
        selected = (
            await session.execute(
                sa.select(
                    assignments_t,
                    probe_groups_t.c.challenge.label("group_challenge"),
                    sa.exists(
                        sa.select(attestations_t.c.id).where(
                            attestations_t.c.assignment_id == assignments_t.c.id,
                            attestations_t.c.validator_id == validator_id,
                            attestations_t.c.authority == "authoritative",
                        )
                    ).label("has_authoritative_attestation"),
                )
                .outerjoin(
                    probe_groups_t,
                    probe_groups_t.c.id == assignments_t.c.probe_group_id,
                )
                .where(assignments_t.c.id == assignment_id)
            )
        ).mappings().first()
        row = dict(selected) if selected else None
        if row and not (row.get("challenge") or {}) and row.get("probe_group_id"):
            row["challenge"] = row.get("group_challenge") or {}

    if not row:
        raise AssignmentError("assignment not found")
    if row["account_id"] != account_id or row["validator_id"] != validator_id:
        raise AssignmentError("assignment not found")
    if claimed.rowcount == 1:
        challenge = row["challenge"] or {}
        if row["modality"] == "image":
            if (
                not media_validation_policy()["enabled"]
                or row["capability"] != "image.fidelity.v1"
                or challenge.get("schema") != "aipg.validator.media.challenge.v1"
                or challenge.get("kind") != "image.fidelity"
            ):
                await _mark_probe(job_id, "failed")
                raise AssignmentError("image probe gate is not authoritative")
            return dict(row), job_id
        if row["modality"] == "video":
            if (
                not video_validation_policy()["enabled"]
                or row["capability"] != "video.contract.v1"
                or challenge.get("schema") != "aipg.validator.media.challenge.v1"
                or challenge.get("kind") != "video.contract"
            ):
                await _mark_probe(job_id, "failed")
                raise AssignmentError("video probe gate is not authoritative")
            return dict(row), job_id
        if row["modality"] != "text":
            await _mark_probe(job_id, "failed")
            raise AssignmentError("unsupported validator probe modality")
        if not str(challenge.get("prompt") or ""):
            await _mark_probe(job_id, "failed")
            raise AssignmentError("assignment has no prompt")
        return dict(row), job_id
    if row["probe_status"] == "completed":
        if row.get("has_authoritative_attestation"):
            raise AssignmentError("assignment attestation already submitted")
        deadline = (
            _aware(row["expires"]) + timedelta(seconds=ATTESTATION_GRACE_SECONDS)
            if row["expires"]
            else now
        )
        if deadline < now:
            raise AssignmentError("assignment has expired")
        stored_result = row.get("probe_result")
        if not isinstance(stored_result, dict):
            raise AssignmentError("assignment probe completed without a replayable result")
        row["_replay_result"] = {
            **_bounded_probe_result(stored_result),
            "replayed": True,
        }
        return row, str(row.get("probe_job_id") or "")
    if row["expires"] and _aware(row["expires"]) < now:
        raise AssignmentError("assignment has expired")
    if row["probe_status"] == "running":
        raise AssignmentError("assignment probe already in progress")
    if int(row["probe_attempts"] or 0) >= PROBE_MAX_ATTEMPTS:
        raise AssignmentError("assignment probe retry limit reached")
    raise AssignmentError("assignment probe is not claimable")


async def _mark_probe(
    job_id: str,
    status: str,
    *,
    prompt_hash: str | None = None,
    response_hash: str | None = None,
    evidence_hash: str | None = None,
    verdict: str | None = None,
    latency_ms: int | None = None,
    result: dict[str, Any] | None = None,
) -> bool:
    stored_result = _bounded_probe_result(result) if result is not None else None
    if status == "completed" and stored_result is None:
        logger.warning(
            "refusing to complete validator probe without durable result job=%s",
            opaque_id(job_id),
        )
        return False
    try:
        async with await new_session() as session:
            values: dict[str, Any] = {
                "probe_status": status,
                "probe_lease_expires": None,
            }
            if prompt_hash is not None:
                values["probe_prompt_hash"] = prompt_hash
            if response_hash is not None:
                values["probe_response_hash"] = response_hash
            if evidence_hash is not None:
                values["probe_evidence_hash"] = evidence_hash
            if verdict is not None:
                values["probe_verdict"] = verdict
            if latency_ms is not None:
                values["probe_latency_ms"] = latency_ms
            if stored_result is not None:
                values["probe_result"] = stored_result
            updated = await session.execute(
                sa.update(assignments_t)
                .where(assignments_t.c.probe_job_id == job_id)
                .values(**values)
            )
            if updated.rowcount != 1:
                await session.rollback()
                return False
            await session.commit()
            return True
    except Exception as exc:
        logger.warning(
            "failed to mark validator probe job=%s status=%s error_type=%s",
            opaque_id(job_id),
            status,
            error_type(exc),
        )
        return False
