# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Offline quote-bound EIP-3009 preparation and signature checking only.

No route accepts this payload yet. A cryptographic match is not funding, EOA
classification, unused-nonce proof, replay exclusion or facilitator approval.
Core never receives a private key or signs. Live contract/domain/code checks,
durable attempts, chain receipts and atomic funded admission remain separate.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_keys.constants import SECPK1_N
from eth_keys.exceptions import BadSignature
from x402.mechanisms.evm.eip712 import build_typed_data_for_signing
from x402.mechanisms.evm.types import ExactEIP3009Authorization, ExactEIP3009Payload

from . import video_quotes as quotes

_HEX_SIGNATURE = re.compile(r"^0x[0-9a-fA-F]{130}$")


class VideoAuthorizationError(ValueError):
    """Generic error only; never reflect a signature or request field."""


@dataclass(frozen=True)
class BoundAuthorization:
    """Cryptographic evidence, deliberately without a paid/funded state."""

    quote_id: str
    job_id: str
    payer: str
    recipient: str
    chain_id: int
    asset: str
    amount_micro: int
    nonce: str
    valid_before: int
    authorization_identity: str


def _now() -> datetime:
    return datetime.now(UTC)


def _live_quote(row: dict) -> dict:
    snapshot = quotes._validate_row(row)
    if snapshot["schema"] != "aipg.video-quote-binding.v2":
        raise VideoAuthorizationError("quote has no frozen signing domain")
    now = int(_now().timestamp())
    if not snapshot["created"] <= now < snapshot["expires"] or now <= 0:
        raise VideoAuthorizationError("quote is outside its signing window")
    return snapshot


def _authorization(snapshot: dict, payer: str) -> ExactEIP3009Authorization:
    payment = snapshot["payment"]
    payer = quotes._address(payer)
    if payer == payment["pay_to"]:
        raise VideoAuthorizationError("self-payment is not external funding")
    nonce = quotes.PreparedVideoQuote(quotes._canonical(snapshot)).values["authorization_nonce"]
    return ExactEIP3009Authorization(from_address=payer, to=payment["pay_to"],
        value=str(payment["amount_micro"]), valid_after=str(payment["valid_after"]),
        valid_before=str(payment["valid_before"]), nonce=nonce)


def _typed_data(snapshot: dict, authorization: ExactEIP3009Authorization) -> dict:
    frozen = snapshot["payment"]["eip712_domain"]
    _domain, types, primary_type, message = build_typed_data_for_signing(
        authorization, frozen["chainId"], frozen["verifyingContract"], frozen["name"], frozen["version"],
    )
    # JSON-ready for eth_signTypedData_v4, without changing the SDK's types.
    message["nonce"] = "0x" + message["nonce"].hex()
    return {"domain": dict(frozen), "types": copy.deepcopy(types),
            "primaryType": primary_type, "message": message}


def signing_request(row: dict, *, payer: str) -> dict:
    """Unsigned offline request; not permission to prompt a real wallet yet."""
    try:
        snapshot = _live_quote(row)
        authorization = _authorization(snapshot, payer)
        return {"authorization": ExactEIP3009Payload(authorization).to_dict()["authorization"],
                "typed_data": _typed_data(snapshot, authorization)}
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise VideoAuthorizationError("video signing request unavailable") from None


def _signature(value: str) -> str:
    if not isinstance(value, str) or not _HEX_SIGNATURE.fullmatch(value):
        raise VideoAuthorizationError("canonical 65-byte signature required")
    raw = bytes.fromhex(value[2:])
    r, s, v = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:64], "big"), raw[64]
    if not 0 < r < SECPK1_N or not 0 < s <= SECPK1_N // 2 or v not in {27, 28}:
        raise VideoAuthorizationError("canonical 65-byte signature required")
    return value


def verify(row: dict, payload: dict, *, expected_payer: str) -> BoundAuthorization:
    """Check the inner exact payload against one owner-loaded immutable quote.

    The future ingress must authenticate/load ownership and validate the outer
    x402 envelope separately. This function never accepts caller-selected chain,
    token, domain, deadline, price, quote owner or executable graph.
    It is an admission-time check, not historical proof for reconciliation after
    expiry. A previously attempted payment still requires independent recovery.
    """
    try:
        request = signing_request(row, payer=expected_payer)
        if type(payload) is not dict or len(payload) != 2 or set(payload) != {"authorization", "signature"}:
            raise VideoAuthorizationError("exact authorization payload required")
        expected, supplied = request["authorization"], payload["authorization"]
        if type(supplied) is not dict or len(supplied) != len(expected) or set(supplied) != set(expected):
            raise VideoAuthorizationError("exact authorization fields required")
        for key, value in expected.items():
            candidate = supplied[key]
            if type(candidate) is not str:
                raise VideoAuthorizationError("canonical string fields required")
            if key in {"from", "to", "nonce"}:
                candidate = candidate.lower()
            if candidate != value:
                raise VideoAuthorizationError("authorization does not match the quote")
        signature = _signature(payload["signature"])
        recovered = Account.recover_message(encode_typed_data(full_message=request["typed_data"]), signature=signature)
        if recovered.lower() != expected["from"]:
            raise VideoAuthorizationError("authorization signer mismatch")
        snapshot = _live_quote(row)  # Cryptographic work cannot extend expiry.
        domain = snapshot["payment"]["eip712_domain"]
        identity = {"schema": "aipg.eip3009-claim.v1", "chain_id": domain["chainId"],
                    "asset": domain["verifyingContract"], "payer": expected["from"], "nonce": expected["nonce"]}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()
        return BoundAuthorization(quote_id=snapshot["quote_id"], job_id=snapshot["job_id"],
            payer=expected["from"], recipient=expected["to"], chain_id=domain["chainId"],
            asset=domain["verifyingContract"], amount_micro=int(expected["value"]), nonce=expected["nonce"],
            valid_before=int(expected["validBefore"]), authorization_identity=digest)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, BadSignature):
        raise VideoAuthorizationError("video authorization does not match a live quote") from None
