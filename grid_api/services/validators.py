# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Validator assignment, attestation, and scorecard storage.

This module is the boundary between preview validator evidence and
assignment-bound evidence. It deliberately does not route production traffic,
reward validators, slash workers, move credits, or write worker payout ledger
rows.
"""

from __future__ import annotations

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
ASSIGNMENT_TTL_SECONDS = int(os.getenv("VALIDATOR_ASSIGNMENT_TTL_SECONDS", "900") or 900)
ATTESTATION_GRACE_SECONDS = max(
    60,
    int(os.getenv("VALIDATOR_ATTESTATION_GRACE_SECONDS", "1800") or 1800),
)
PROBE_TIMEOUT_SECONDS = int(os.getenv("VALIDATOR_PROBE_TIMEOUT_SECONDS", "180") or 180)
PROBE_LATENCY_BUDGET_SECONDS = int(os.getenv("VALIDATOR_PROBE_LATENCY_BUDGET_SECONDS", "30") or 30)
PROBE_MAX_ATTEMPTS = max(1, int(os.getenv("VALIDATOR_PROBE_MAX_ATTEMPTS", "2") or 2))
PROBE_LEASE_SECONDS = max(
    PROBE_TIMEOUT_SECONDS + 30,
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

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SIG_RE = re.compile(r"^(0x)?[0-9a-fA-F]{130}$")


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
    async with await new_session() as session:
        await session.execute(
            sa.update(validators_t)
            .where(validators_t.c.id == validator["id"], validators_t.c.status == "active")
            .values(
                software_version=software_version,
                capabilities=normalized_capabilities,
                last_heartbeat=now,
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


_TEXT_CHALLENGE_KINDS = (
    "echo",
    "math",
    "json.object",
    "context.retrieve",
    "logic.steps",
    "tool.call",
)
_TEXT_CHALLENGE_CAPABILITIES = {
    "echo": "text.instruction.v1",
    "math": "text.reasoning.v1",
    "json.object": "text.structured.v1",
    "context.retrieve": "text.context.4k.v1",
    "logic.steps": "text.reasoning.multistep.v1",
    "tool.call": "text.tool_call.v1",
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


def _make_text_challenge(kind: str | None = None) -> dict[str, Any]:
    """Create one private, randomized text challenge.

    The assignment carries only a one-way expected-answer commitment. The
    optional kind is for deterministic tests; production chooses with
    cryptographic randomness so worker order cannot fingerprint a family.
    """
    selected = kind or secrets.choice(_TEXT_CHALLENGE_KINDS)
    if selected not in _TEXT_CHALLENGE_KINDS:
        raise ValueError("unsupported text challenge kind")

    if selected == "echo":
        token = secrets.token_hex(8).upper()
        prompt = f"Reply with exactly this token and nothing else: {token}"
        expected = token
        kind = "echo"
        capability = "text.instruction.v1"
    elif selected == "math":
        a = secrets.randbelow(80) + 11
        b = secrets.randbelow(80) + 11
        if secrets.randbelow(2):
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
    elif selected == "context.retrieve":
        record_count = 180
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
        kind = "context.retrieve"
        capability = "text.context.4k.v1"
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
    else:
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
    challenge = {
        "kind": kind,
        "prompt": prompt,
        "expected_hash": _hash_text(expected),
        "max_tokens": PROBE_MAX_TOKENS,
        "temperature": 0,
        "capability": capability,
    }
    if selected == "tool.call":
        challenge["tools"] = tools
        challenge["tool_choice"] = tool_choice
    return challenge


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


def _normalized_text_answer(kind: str, text: str, tool_calls: Any = None) -> str | None:
    answer = _strip_think(text)
    if kind == "tool.call":
        if answer:
            return None
        return _normalized_tool_call(tool_calls)
    if not answer:
        return None
    if kind in ("echo", "context.retrieve"):
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


def _score_text_challenge(
    challenge: dict[str, Any], text: str, latency_ms: int, *, tool_calls: Any = None
) -> str:
    expected_hash = str(challenge.get("expected_hash") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        return "failed"
    candidate = _normalized_text_answer(str(challenge.get("kind") or ""), text, tool_calls)
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
) -> dict[str, Any]:
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
    }
    if include_grid_nonce:
        out["grid_nonce"] = row["grid_nonce"]
    if include_challenge:
        challenge = row["challenge"] or {}
        out["challenge"] = {
            key: challenge[key]
            for key in (
                "kind", "prompt", "expected_hash", "max_tokens", "temperature",
                "tools", "tool_choice",
            )
            if key in challenge
        }
    return out


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


async def issue_assignments(
    *,
    account_id,
    validator_id: str,
    validator_wallet: str | None,
    active_workers: list[dict[str, Any]],
    limit: int = 5,
    modality: str = "text",
) -> dict[str, Any]:
    """Return this validator's work from shared, economically inert probe groups."""
    safe_limit = max(1, min(int(limit), 25))
    if modality != "text":
        raise AssignmentError("only text assignments are enabled in this rollout")

    now = _now()
    expires = now + timedelta(seconds=ASSIGNMENT_TTL_SECONDS)
    wallet = validator_wallet.lower() if validator_wallet and _ADDR_RE.match(validator_wallet) else None

    async with await new_session() as session:
        # Serialize concurrent polls from the same registered validator. The DB
        # uniqueness guard remains the final protection on group membership.
        validator_row = (
            await session.execute(
                sa.select(validators_t.c.id, validators_t.c.capabilities)
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
                    assignments_t.c.status != "finalized",
                    assignments_t.c.probe_status != "completed",
                    assignments_t.c.expires >= now,
                )
                .order_by(assignments_t.c.created.asc())
                .limit(safe_limit)
            )
        ).mappings().all()
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
            models = [m for m in (worker.get("models") or []) if isinstance(m, str) and m]
            if not models:
                continue
            model = models[0]
            if (worker_id, model) in existing_keys:
                continue

            if session.bind and session.bind.dialect.name == "postgresql":
                lock_key = int.from_bytes(
                    hashlib.sha256(
                        f"validator-group:{worker_id}:{model}:{modality}".encode()
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
                        probe_groups_t.c.model == model,
                        probe_groups_t.c.modality == modality,
                        probe_groups_t.c.expires >= now,
                        probe_groups_t.c.quorum_status != "finalized",
                    )
                    .order_by(probe_groups_t.c.created.asc())
                )
            ).mappings().all()
            group = None
            unfilled_group_exists = False
            for candidate in candidate_groups:
                if candidate["capability"] not in supported_capabilities:
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
                unfilled_group_exists = True
                already_assigned = await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(assignments_t)
                    .where(
                        assignments_t.c.probe_group_id == candidate["id"],
                        assignments_t.c.validator_id == validator_id,
                    )
                )
                if not already_assigned:
                    group = candidate
                    break

            # Do not let one fast validator manufacture many nominally shared
            # groups while the current group is still waiting for peers.
            if group is None and unfilled_group_exists:
                continue

            if group is None:
                challenge = _make_text_challenge(secrets.choice(challenge_kinds))
                group_id = f"prg_{uuid4().hex}"
                group_values = {
                    "id": group_id,
                    "target_worker_id": worker_id,
                    "target_worker_name": worker_name,
                    "model": model,
                    "modality": "text",
                    "capability": challenge["capability"],
                    "canary_kind": challenge["kind"],
                    "scoring_policy_id": "text.generated.v4",
                    "challenge": challenge,
                    "challenge_hash": _hash_obj({
                        "group_id": group_id,
                        "worker_id": worker_id,
                        "model": model,
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
            else:
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
            await session.execute(sa.insert(assignments_t).values(**values))
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


async def _network_health_in_session(session, *, since_hours: int) -> dict[str, Any]:
    """Return privacy-preserving aggregate validator network health."""
    cutoff = _now() - timedelta(hours=since_hours)
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
                validators_t.c.last_heartbeat
                >= _now() - timedelta(seconds=VALIDATOR_HEARTBEAT_FRESH_SECONDS),
            )
            .group_by(validators_t.c.software_version)
            .order_by(sa.func.count().desc(), validators_t.c.software_version.asc())
            .limit(20)
        )
    ).mappings().all()

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
            "verified": 0,
            "proven": False,
            "status": "not_yet_verified",
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
            "operator_independence_proven": False,
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
            sa.func.avg(attestations_t.c.latency_ms).label("avg_latency_ms"),
            sa.func.avg(attestations_t.c.score).label("avg_score"),
            sa.func.min(attestations_t.c.created).label("first_seen"),
            sa.func.max(attestations_t.c.created).label("last_seen"),
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
        subject_type = "worker" if row["worker_id"] else "model"
        subject_id = row["worker_id"] or row["model"] or "unknown"
        items.append({
            "subject_type": subject_type,
            "subject_id": subject_id,
            "worker_id": row["worker_id"],
            "model": row["model"],
            "modality": row["modality"],
            "capability": row["capability"],
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
    response. It never reserves credits, writes ledger rows, pays den, strikes,
    or slashes. The caller can use the returned hashes in a signed attestation.
    """
    from . import job_queue, token_stream

    row, job_id = await _claim_probe_lease(
        account_id=account_id,
        validator_id=validator_id,
        assignment_id=assignment_id,
    )
    challenge = row["challenge"] or {}
    prompt = str(challenge.get("prompt") or "")
    payload = {
        "request": {
            "model": row["model"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": int(challenge.get("max_tokens") or 32),
            "temperature": float(challenge.get("temperature") or 0),
            "stream": True,
        },
        "api_format": "openai-chat",
        "prompt": prompt,
        "max_length": int(challenge.get("max_tokens") or 32),
        "temperature": float(challenge.get("temperature") or 0),
        "_validator_probe": True,
        "_validator_assignment_id": assignment_id,
        "_validator_probe_group_id": row["probe_group_id"],
        "_validator_grid_nonce": row["grid_nonce"],
    }
    for key in ("tools", "tool_choice"):
        if key in challenge:
            payload["request"][key] = challenge[key]

    started = _now()
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
        await _mark_probe(job_id, "failed")
        logger.error(
            "validator probe dispatch failed assignment=%s job=%s error_type=%s",
            opaque_id(assignment_id),
            opaque_id(job_id),
            error_type(exc),
        )
        return {
            "status": "error",
            "assignment_id": assignment_id,
            "job_id": job_id,
            "message": "probe dispatch failed",
            "code": 503,
        }

    chunks: list[str] = []
    full_text = ""
    grid_meta = None
    usage = None
    tool_calls = None
    finish_reason = None
    try:
        async for event in token_stream.subscribe_tokens(job_id, timeout=PROBE_TIMEOUT_SECONDS):
            if event.get("error"):
                await _mark_probe(job_id, "failed")
                return {
                    "status": "error",
                    "assignment_id": assignment_id,
                    "job_id": job_id,
                    "message": event.get("error", "probe failed"),
                    "code": event.get("code", 502),
                }
            if event.get("text") == token_stream.DONE_SENTINEL:
                full_text = event.get("full_text") or "".join(chunks)
                usage = event.get("usage")
                grid_meta = event.get("grid")
                tool_calls = event.get("tool_calls")
                finish_reason = event.get("finish_reason")
                break
            chunks.append(token_stream.event_content_text(event))
        else:
            await _mark_probe(job_id, "timeout")
            return {
                "status": "error",
                "assignment_id": assignment_id,
                "job_id": job_id,
                "message": "probe timed out",
                "code": 504,
            }
    except Exception as exc:
        await _mark_probe(job_id, "failed")
        logger.error(
            "validator probe failed assignment=%s job=%s error_type=%s",
            opaque_id(assignment_id),
            opaque_id(job_id),
            error_type(exc),
        )
        raise

    response_commitment = (
        _canonical({"text": full_text, "tool_calls": tool_calls})
        if str(challenge.get("kind") or "") == "tool.call"
        else full_text
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
        "prompt_hash": _hash_text(str((row["challenge"] or {}).get("prompt") or "")),
        "response_hash": _hash_text(response_commitment),
    }
    evidence["evidence_hash"] = _hash_obj(evidence)
    latency_ms = int((_now() - started).total_seconds() * 1000)
    probe_verdict = _score_text_challenge(
        row["challenge"] or {}, full_text, latency_ms, tool_calls=tool_calls
    )
    await _mark_probe(
        job_id,
        "completed",
        prompt_hash=evidence["prompt_hash"],
        response_hash=evidence["response_hash"],
        evidence_hash=evidence["evidence_hash"],
        verdict=probe_verdict,
        latency_ms=latency_ms,
    )
    return {
        "status": "completed",
        "assignment_id": assignment_id,
        "probe_group_id": row["probe_group_id"],
        "job_id": job_id,
        "grid_nonce": row["grid_nonce"],
        "target_worker_id": row["target_worker_id"],
        "target_worker_name": row["target_worker_name"],
        "model": row["model"],
        "modality": row["modality"],
        "capability": row["capability"],
        "canary_kind": row["canary_kind"],
        "output_text": full_text,
        "tool_calls": tool_calls,
        "finish_reason": finish_reason,
        "usage": usage,
        "grid": grid_meta,
        "probe_latency_ms": latency_ms,
        **evidence,
        "economic_effect": "none",
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
                probe_lease_expires=lease_expires,
                probed=now,
            )
        )
        await session.commit()
        row = (
            await session.execute(
                sa.select(assignments_t).where(assignments_t.c.id == assignment_id)
            )
        ).mappings().first()

    if not row:
        raise AssignmentError("assignment not found")
    if row["account_id"] != account_id or row["validator_id"] != validator_id:
        raise AssignmentError("assignment not found")
    if claimed.rowcount == 1:
        challenge = row["challenge"] or {}
        if row["modality"] != "text":
            await _mark_probe(job_id, "failed")
            raise AssignmentError("only text probes are enabled in this rollout")
        if not str(challenge.get("prompt") or ""):
            await _mark_probe(job_id, "failed")
            raise AssignmentError("assignment has no prompt")
        return dict(row), job_id
    if row["expires"] and _aware(row["expires"]) < now:
        raise AssignmentError("assignment has expired")
    if row["probe_status"] == "completed":
        raise AssignmentError("assignment probe already completed")
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
) -> None:
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
            await session.execute(
                sa.update(assignments_t)
                .where(assignments_t.c.probe_job_id == job_id)
                .values(**values)
            )
            await session.commit()
    except Exception as exc:
        logger.warning(
            "failed to mark validator probe job=%s status=%s error_type=%s",
            opaque_id(job_id),
            status,
            error_type(exc),
        )
