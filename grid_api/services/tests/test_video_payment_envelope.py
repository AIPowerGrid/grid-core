# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Full v2 SDK envelope controls, not received funds or a public pay API."""

import base64
import copy
import json
from datetime import timedelta

import httpx
import pytest
from eth_account import Account
from web3 import Web3
from x402.http.facilitator_client import HTTPFacilitatorClient
from x402.http.facilitator_client_base import FacilitatorConfig
from x402.http.utils import encode_payment_signature_header
from x402.schemas import PaymentPayload, PaymentRequirements, SettleRequest, VerifyRequest

from grid_api.services import video_payment_envelope as envelopes
from grid_api.services.tests import test_video_eip3009 as support

prepared = support.prepared
admission = support.admission
registry = support.registry
synthetic_dependencies = support.synthetic_dependencies


def envelope(row, account, *, resource_url=None):
    result = {"x402Version": 2, "accepted": envelopes.requirements(row), "payload": support.sign(row, account)}
    if resource_url is not None:
        result["resource"] = {"url": resource_url, "description": "Grid video quote", "mimeType": "application/json"}
    return result


@pytest.mark.parametrize("has_resource", [False, True])
def test_exact_requirements_and_sdk_body_without_authority_or_network(prepared, has_resource, monkeypatch):
    row, account = prepared
    resource = f"https://api.example/v1/videos/quotes/{row['id']}" if has_resource else None
    supplied = envelope(row, account, resource_url=resource)
    original = copy.deepcopy(supplied)
    def no_client(*_args, **_kwargs):
        raise AssertionError("pure envelope must not create a network client")
    monkeypatch.setattr(httpx, "Client", no_client)
    monkeypatch.setattr(httpx, "AsyncClient", no_client)
    verified = envelopes.validate(row, supplied, expected_payer=account.address, resource_url=resource)
    assert verified.quote_id == str(row["id"]) and verified.amount_micro == row["amount_micro"]
    assert not hasattr(verified, "signature") and not hasattr(verified, "funded")
    assert supplied["payload"]["signature"] not in repr(verified)
    body = envelopes.facilitator_request(row, supplied, expected_payer=account.address, resource_url=resource)
    sdk_payload = PaymentPayload.model_validate(supplied)
    required = PaymentRequirements.model_validate(supplied["accepted"])
    client = HTTPFacilitatorClient(FacilitatorConfig(url="https://facilitator.example"))
    assert body == client._build_request_body(2, sdk_payload.model_dump(by_alias=True, exclude_none=True),
                                             required.model_dump(by_alias=True, exclude_none=True))
    assert VerifyRequest.model_validate(body).model_dump(by_alias=True, exclude_none=True) == body
    assert SettleRequest.model_validate(body).model_dump(by_alias=True, exclude_none=True) == body
    assert body["paymentRequirements"]["extra"]["assetTransferMethod"] == "eip3009"
    assert body["paymentRequirements"]["maxTimeoutSeconds"] == support.quotes.TTL_SECONDS
    assert supplied == original
    body["paymentPayload"]["payload"]["authorization"]["value"] = "1"
    body["paymentRequirements"]["extra"]["name"] = "changed"
    assert supplied == original and envelopes.requirements(row) == original["accepted"]


@pytest.mark.parametrize("field,value", [
    ("scheme", "upto"), ("network", "eip155:1"), ("asset", "0x" + "3" * 40),
    ("amount", "1"), ("amount", 7500), ("amount", "07500"), ("payTo", "0x" + "3" * 40),
    ("maxTimeoutSeconds", 301), ("maxTimeoutSeconds", "300"), ("maxTimeoutSeconds", True),
    ("extra", {}), ("extra", {"name": "USD Coin", "version": "1", "assetTransferMethod": "eip3009"}),
    ("extra", {"name": "USD Coin", "version": "2", "assetTransferMethod": "permit2"}),
    ("extra", {"name": "USD Coin", "version": "2", "assetTransferMethod": "eip3009", "discount": 0}),
])
def test_client_requirements_cannot_replace_server_quote(prepared, field, value):
    row, account = prepared
    supplied = envelope(row, account)
    supplied["accepted"][field] = value
    with pytest.raises(envelopes.VideoEnvelopeError):
        envelopes.facilitator_request(row, supplied, expected_payer=account.address)


@pytest.mark.parametrize("fault", ["v1", "version-string", "version-bool", "root-extra", "extension",
    "missing-field", "requirements-extra", "legacy-amount", "inner-extra", "unsigned", "wrong-payer",
    "wrong-nonce", "wrong-signer", "raw-inner", "unexpected-resource", "null-resource"])
def test_noncanonical_or_unbound_payload_rejects_without_echoing_capability(prepared, fault, caplog):
    row, account = prepared
    supplied = envelope(row, account)
    signature = supplied["payload"]["signature"]
    if fault in {"v1", "version-string", "version-bool"}:
        supplied["x402Version"] = {"v1": 1, "version-string": "2", "version-bool": True}[fault]
    elif fault in {"root-extra", "extension"}:
        supplied["accountId" if fault == "root-extra" else "extensions"] = {"quote": str(row["id"])}
    elif fault == "missing-field":
        supplied.pop("accepted")
    elif fault == "requirements-extra":
        supplied["accepted"]["discount"] = 0
    elif fault == "legacy-amount":
        supplied["accepted"]["maxAmountRequired"] = supplied["accepted"].pop("amount")
    elif fault == "inner-extra":
        supplied["payload"]["private_key"] = signature
    elif fault == "unsigned":
        supplied["payload"].pop("signature")
    elif fault == "wrong-payer":
        supplied["payload"]["authorization"]["from"] = "0x" + "3" * 40
    elif fault == "wrong-nonce":
        supplied["payload"]["authorization"]["nonce"] = "0x" + "3" * 64
    elif fault == "wrong-signer":
        supplied["payload"] = support.sign(row, Account.create())
    elif fault == "raw-inner":
        supplied = supplied["payload"]
    else:
        supplied["resource"] = None if fault == "null-resource" else {"url": "https://attacker.example"}
    with pytest.raises(envelopes.VideoEnvelopeError) as caught:
        envelopes.facilitator_request(row, supplied, expected_payer=account.address)
    assert signature not in str(caught.value) + repr(caught.value) + caplog.text
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("fault", ["url", "description", "mimeType", "missing", "extra"])
def test_resource_descriptor_is_operator_selected_not_payment_authority(prepared, fault):
    row, account = prepared
    url = f"https://api.example/v1/videos/quotes/{row['id']}"
    supplied = envelope(row, account, resource_url=url)
    if fault == "missing":
        supplied.pop("resource")
    elif fault == "extra":
        supplied["resource"]["tags"] = ["untrusted"]
    else:
        supplied["resource"][fault] = "https://attacker.example" if fault == "url" else "untrusted"
    with pytest.raises(envelopes.VideoEnvelopeError):
        envelopes.validate(row, supplied, expected_payer=account.address, resource_url=url)


@pytest.mark.parametrize("url", ["http://api.example", "https://name:secret@api.example", "https://api.example?key=x",
    "https://api.example#fragment", "https://api.example/ bad", "https://api.example/\n",
    "https://api.example:invalid", "https://api.example:0", 1])
def test_resource_credentials_or_unreviewed_shapes_reject(prepared, url):
    row, account = prepared
    with pytest.raises(envelopes.VideoEnvelopeError):
        envelopes.validate(row, envelope(row, account), expected_payer=account.address, resource_url=url)


def test_checksum_addresses_do_not_change_canonical_requirements(prepared):
    row, account = prepared
    supplied = envelope(row, account)
    for field in ("asset", "payTo"):
        supplied["accepted"][field] = Web3.to_checksum_address(supplied["accepted"][field])
    result = envelopes.facilitator_request(row, supplied, expected_payer=account.address)
    assert result["paymentRequirements"] == envelopes.requirements(row)


@pytest.mark.parametrize("entry", ["validate", "facilitator_request"])
def test_input_changes_during_validation_cannot_rebind_the_snapshot(prepared, entry, monkeypatch):
    row, account = prepared
    supplied = envelope(row, account)
    original_row, original_envelope = copy.deepcopy(row), copy.deepcopy(supplied)
    original = envelopes._requirements
    def mutate(frozen):
        result = original(frozen)
        supplied["accepted"]["amount"] = "1"
        supplied["payload"]["authorization"]["value"] = "1"
        row["snapshot"]["payment"]["pay_to"] = "0x" + "3" * 40
        return result
    monkeypatch.setattr(envelopes, "_requirements", mutate)
    result = getattr(envelopes, entry)(row, supplied, expected_payer=account.address)
    if entry == "validate":
        assert result.amount_micro == original_row["amount_micro"]
        assert result.recipient == original_row["snapshot"]["payment"]["pay_to"]
    else:
        assert result["paymentRequirements"] == original_envelope["accepted"]
        assert result["paymentPayload"]["payload"] == original_envelope["payload"]
    assert supplied != original_envelope and row != original_row


def test_expiry_during_serialization_does_not_extend_the_quote(prepared, monkeypatch):
    row, account = prepared
    supplied = envelope(row, account)
    original = SettleRequest.model_dump
    def expire(self, **kwargs):
        result = original(self, **kwargs)
        monkeypatch.setattr(envelopes.bindings, "_now", lambda: support.quotes._aware(row["expires"]) + timedelta(seconds=1))
        return result
    monkeypatch.setattr(SettleRequest, "model_dump", expire)
    with pytest.raises(envelopes.VideoEnvelopeError):
        envelopes.facilitator_request(row, supplied, expected_payer=account.address)


@pytest.mark.parametrize("has_resource", [False, True])
def test_actual_sdk_payment_signature_header_round_trips(prepared, has_resource):
    row, account = prepared
    url = f"https://api.example/v1/videos/quotes/{row['id']}" if has_resource else None
    supplied = envelope(row, account, resource_url=url)
    header = encode_payment_signature_header(PaymentPayload.model_validate(supplied))
    assert envelopes.facilitator_request_from_header(row, header, expected_payer=account.address, resource_url=url) == (
        envelopes.facilitator_request(row, supplied, expected_payer=account.address, resource_url=url))


@pytest.mark.parametrize("fault", ["empty", "too-large", "non-ascii", "spaces", "multiple", "pad-bits",
    "invalid-utf8", "invalid-json", "array", "duplicate-root", "duplicate-nested", "nonfinite", "float",
    "deep", "bool-version", "omitted-version"])
def test_wire_ambiguity_or_parser_failure_cannot_be_coerced(prepared, fault, caplog):
    row, account = prepared
    value = envelope(row, account)
    signature = value["payload"]["signature"]
    raw = json.dumps(value).encode()
    if fault == "invalid-utf8":
        raw = b"\xff"
    elif fault == "invalid-json":
        raw = b"{"
    elif fault == "array":
        raw = b"[]"
    elif fault == "duplicate-root":
        raw = b'{"x402Version":2,' + raw[1:]
    elif fault == "duplicate-nested":
        raw = raw.replace(b'"scheme": "exact"', b'"scheme":"upto","scheme":"exact"')
    elif fault == "nonfinite":
        raw = raw.replace(b'"maxTimeoutSeconds": 300', b'"maxTimeoutSeconds": NaN')
    elif fault == "float":
        raw = raw.replace(b'"maxTimeoutSeconds": 300', b'"maxTimeoutSeconds": 300.0')
    elif fault == "deep":
        raw = b"[" * 1500 + b"0" + b"]" * 1500
    elif fault in {"bool-version", "omitted-version"}:
        if fault == "bool-version":
            value["x402Version"] = True
        else:
            value.pop("x402Version")
        raw = json.dumps(value).encode()
    header = base64.b64encode(raw).decode()
    if fault in {"empty", "too-large", "non-ascii", "pad-bits"}:
        header = {"empty": "", "too-large": "A" * (envelopes.MAX_HEADER_CHARS + 1),
            "non-ascii": "\u00e9", "pad-bits": "e31="}[fault]
    elif fault == "spaces":
        header += "\n"
    elif fault == "multiple":
        header += "," + header
    with pytest.raises(envelopes.VideoEnvelopeError) as caught:
        envelopes.facilitator_request_from_header(row, header, expected_payer=account.address)
    assert signature not in str(caught.value) + repr(caught.value) + caplog.text
    assert caught.value.__suppress_context__
