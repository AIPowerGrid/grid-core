# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Strict quote-bound x402 v2 envelope, without sending or funding authority.

No route consumes this candidate. Incoming extensions and resource metadata are
not payment authority. An outbound body contains a spending capability: never
log/persist it, and never submit it without winning the durable attempt first.
"""

from __future__ import annotations

import base64
import copy
import json
from urllib.parse import urlsplit

from x402.schemas import PaymentPayload, PaymentRequirements, ResourceInfo, SettleRequest

from . import video_eip3009 as bindings
from . import video_quotes as quotes

MAX_HEADER_CHARS = 8192


class VideoEnvelopeError(ValueError):
    """Generic refusal; no signed payload, resource or request values."""


def _requirements(row: dict) -> PaymentRequirements:
    snapshot = bindings._live_quote(row)
    payment, domain = snapshot["payment"], snapshot["payment"]["eip712_domain"]
    return PaymentRequirements(scheme="exact", network=payment["network"], asset=payment["asset"],
        amount=str(payment["amount_micro"]), pay_to=payment["pay_to"],
        max_timeout_seconds=snapshot["expires"] - snapshot["created"],
        extra={"name": domain["name"], "version": domain["version"], "assetTransferMethod": "eip3009"})


def requirements(row: dict) -> dict:
    """Fresh unsigned server requirements; not permission to prompt a wallet."""
    try:
        return _requirements(row).model_dump(by_alias=True, exclude_none=True)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise VideoEnvelopeError("video payment requirements unavailable") from None


def _resource(resource_url: str | None) -> ResourceInfo | None:
    if resource_url is None:
        return None
    if type(resource_url) is not str or not 0 < len(resource_url) <= 2048:
        raise VideoEnvelopeError("operator resource unavailable")
    url = urlsplit(resource_url)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or (url.port is not None and not 1 <= url.port <= 65535)
            or any(char.isspace() or ord(char) < 32 for char in resource_url)):
        raise VideoEnvelopeError("operator resource unavailable")
    return ResourceInfo(url=resource_url, description="Grid video quote", mime_type="application/json")


def validate(row: dict, envelope: dict, *, expected_payer: str,
             resource_url: str | None = None) -> bindings.BoundAuthorization:
    """Verify exact outer requirements and original inner signature offline.

    Ownership/authentication is the caller's duty; neither a client URL nor a
    Host header may select resource_url. Optional metadata must exactly match
    that operator-controlled descriptor; extensions remain unsupported.
    """
    try:
        row, envelope = copy.deepcopy(row), copy.deepcopy(envelope)
        expected = requirements(row)
        resource = _resource(resource_url)
        fields = {"x402Version", "accepted", "payload"} | ({"resource"} if resource is not None else set())
        if (type(envelope) is not dict or set(envelope) != fields
                or type(envelope["x402Version"]) is not int or envelope["x402Version"] != 2):
            raise VideoEnvelopeError("unsupported video payment envelope")
        accepted = envelope["accepted"]
        if type(accepted) is not dict or set(accepted) != set(expected):
            raise VideoEnvelopeError("video requirements changed")
        normalized = {}
        for key, value in expected.items():
            candidate = accepted[key]
            if type(candidate) is not type(value):
                raise VideoEnvelopeError("video requirements changed")
            if key == "extra":
                if set(candidate) != set(value) or any(type(item) is not str for item in candidate.values()):
                    raise VideoEnvelopeError("video requirements changed")
            if key in {"asset", "payTo"}:
                candidate = quotes._address(candidate)
            normalized[key] = candidate
        if normalized != expected:
            raise VideoEnvelopeError("video requirements changed")
        if resource is not None:
            descriptor = resource.model_dump(by_alias=True, exclude_none=True)
            supplied = envelope["resource"]
            if (type(supplied) is not dict or set(supplied) != set(descriptor)
                    or any(type(item) is not str for item in supplied.values()) or supplied != descriptor):
                raise VideoEnvelopeError("video resource changed")
        return bindings.verify(row, envelope["payload"], expected_payer=expected_payer)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise VideoEnvelopeError("video payment envelope does not match its live quote") from None


def facilitator_request(row: dict, envelope: dict, *, expected_payer: str,
                        resource_url: str | None = None) -> dict:
    """SDK-shaped verify/settle body only; no client, send, SQL or grant.

    The signature stays solely in the returned ephemeral request. Winning
    journal begin, ready inputs, independent receipt proof and atomic funded
    admission remain separate mandatory boundaries. Replay is not send consent.
    """
    try:
        row, envelope = copy.deepcopy(row), copy.deepcopy(envelope)
        validate(row, envelope, expected_payer=expected_payer, resource_url=resource_url)
        required = _requirements(row)
        payload = PaymentPayload(x402_version=2, accepted=required,
            payload=copy.deepcopy(envelope["payload"]), resource=_resource(resource_url))
        body = SettleRequest(x402_version=2, payment_payload=payload,
            payment_requirements=required).model_dump(by_alias=True, exclude_none=True)
        bindings._live_quote(row)
        return body
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise VideoEnvelopeError("video facilitator request unavailable") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise VideoEnvelopeError("ambiguous payment envelope")
        result[key] = value
    return result


def _reject_number(_value: str):
    raise VideoEnvelopeError("noncanonical payment number")


def facilitator_request_from_header(row: dict, header: str, *, expected_payer: str,
                                    resource_url: str | None = None) -> dict:
    """Bounded PAYMENT-SIGNATURE decoding; no legacy/fallback/coercion.

    Parsed data is checked before SDK model construction. Header processing
    alone does not authenticate, begin an attempt, send or create a job.
    """
    try:
        if type(header) is not str or not 0 < len(header) <= MAX_HEADER_CHARS:
            raise VideoEnvelopeError("bounded payment header required")
        raw = base64.b64decode(header.encode("ascii"), validate=True)
        if base64.b64encode(raw).decode("ascii") != header:
            raise VideoEnvelopeError("canonical payment header required")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=_reject_number, parse_float=_reject_number)
        return facilitator_request(row, value, expected_payer=expected_payer, resource_url=resource_url)
    except (ValueError, TypeError, RecursionError):
        raise VideoEnvelopeError("video payment header unavailable") from None
