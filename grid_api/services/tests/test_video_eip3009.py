# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real offline ECDSA/SDK compatibility; not funded or deployable payments."""

import copy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from eth_keys.constants import SECPK1_N
from x402.mechanisms.evm.eip712 import build_typed_data_for_signing, hash_eip3009_authorization
from x402.mechanisms.evm.exact.client import ExactEvmScheme
from x402.mechanisms.evm.signers import EthAccountSigner
from x402.mechanisms.evm.types import ExactEIP3009Authorization, TypedDataField
from x402.schemas import PaymentRequirements

from grid_api.routers import videos
from grid_api.services import pricing, video_plans
from grid_api.services import video_eip3009 as bindings
from grid_api.services import video_quotes as quotes
from grid_api.services.tests import test_media_job_journal as journal_tests
from grid_api.services.tests import test_video_plans as plan_tests

synthetic_dependencies = journal_tests.synthetic_dependencies
admission = plan_tests.admission
registry = plan_tests.registry
mp4 = plan_tests.mp4


@pytest_asyncio.fixture(params=["eip155:8453", "eip155:84532"])
async def prepared(request, admission):
    inputs = await videos.validate_video(plan_tests.request("generated"), {}, async_submission=True)
    quote = quotes.prepare(account_id=uuid4(), idempotency_key=str(uuid4()), request_hash="a" * 64,
        plan=video_plans.compile_video(inputs), max_cost_micro=100_000, server_max_micro=100_000,
        network=request.param, pay_to="0x" + "1" * 40)
    return quote.values, Account.create()


def sign(row, signer):
    request = bindings.signing_request(row, payer=signer.address)
    signed = signer.sign_message(encode_typed_data(full_message=request["typed_data"]))
    return {"authorization": request["authorization"], "signature": "0x" + signed.signature.hex()}


def test_exact_authorization_has_quote_nonce_and_domain_and_retains_no_signature(prepared):
    row, signer = prepared
    payload = sign(row, signer)
    verified = bindings.verify(row, payload, expected_payer=signer.address)
    assert verified.quote_id == str(row["id"]) and verified.job_id == str(row["job_id"])
    assert verified.nonce == row["authorization_nonce"]
    assert verified.amount_micro == 7500 and verified.payer == signer.address.lower()
    assert len(verified.authorization_identity) == 64
    assert payload["signature"] not in repr(verified)
    assert not hasattr(verified, "funded") and not hasattr(verified, "signature")
    # This pure check intentionally does not claim replay protection.
    assert bindings.verify(row, payload, expected_payer=signer.address) == verified
    typed = bindings.signing_request(row, payer=signer.address)["typed_data"]
    assert typed["domain"] == row["snapshot"]["payment"]["eip712_domain"]
    assert typed["primaryType"] == "TransferWithAuthorization"
    assert [field["name"] for field in typed["types"]["EIP712Domain"]] == [
        "name", "version", "chainId", "verifyingContract"]
    assert typed["message"]["nonce"] == row["authorization_nonce"]


def test_opt_in_before_signing_matches_the_locked_official_sdk(prepared):
    row, account = prepared
    request = bindings.signing_request(row, payer=account.address)
    auth = request["authorization"]
    authorization = ExactEIP3009Authorization(from_address=auth["from"], to=auth["to"], value=auth["value"],
        valid_after=auth["validAfter"], valid_before=auth["validBefore"], nonce=auth["nonce"])
    domain = row["snapshot"]["payment"]["eip712_domain"]
    sdk_domain, types, primary, message = build_typed_data_for_signing(authorization, domain["chainId"],
        domain["verifyingContract"], domain["name"], domain["version"])
    fields = {name: [TypedDataField(**field) for field in values] for name, values in types.items()}
    signature = EthAccountSigner(account).sign_typed_data(sdk_domain, fields, primary, message)
    payload = {"authorization": auth, "signature": "0x" + signature.hex()}
    assert payload == sign(row, account)
    signable = encode_typed_data(full_message=request["typed_data"])
    from eth_utils import keccak
    assert keccak(b"\x19" + signable.version + signable.header + signable.body) == hash_eip3009_authorization(
        authorization, domain["chainId"], domain["verifyingContract"], domain["name"], domain["version"])
    assert bindings.verify(row, payload, expected_payer=account.address).nonce == row["authorization_nonce"]


def test_stock_exact_client_nonce_is_rejected_not_silently_rebound(prepared):
    row, account = prepared
    payment = row["snapshot"]["payment"]
    domain = payment["eip712_domain"]
    requirements = PaymentRequirements(scheme="exact", network=payment["network"], asset=payment["asset"],
        amount=str(payment["amount_micro"]), pay_to=payment["pay_to"], max_timeout_seconds=300,
        extra={"name": domain["name"], "version": domain["version"], "assetTransferMethod": "eip3009"})
    payload = ExactEvmScheme(EthAccountSigner(account)).create_payment_payload(requirements)
    assert payload["authorization"]["nonce"] != row["authorization_nonce"]
    with pytest.raises(bindings.VideoAuthorizationError):
        bindings.verify(row, payload, expected_payer=account.address)
    # Replacing an already-signed nonce is invalid, even with otherwise exact fields.
    payload["authorization"] = bindings.signing_request(row, payer=account.address)["authorization"]
    with pytest.raises(bindings.VideoAuthorizationError):
        bindings.verify(row, payload, expected_payer=account.address)


@pytest.mark.parametrize("field,value", [("from", "0x" + "2" * 40), ("to", "0x" + "2" * 40),
    ("value", "7501"), ("value", "07500"), ("value", 7500), ("validAfter", "1"),
    ("validBefore", "9999999999"), ("nonce", "0x" + "2" * 64)])
def test_changed_authorization_fields_fail_even_if_the_attacker_signs_them(prepared, field, value):
    row, account = prepared
    payload = sign(row, account)
    payload["authorization"][field] = value
    typed = bindings.signing_request(row, payer=account.address)["typed_data"]
    typed["message"][field] = value
    signed = account.sign_message(encode_typed_data(full_message=typed))
    payload["signature"] = "0x" + signed.signature.hex()
    with pytest.raises(bindings.VideoAuthorizationError):
        bindings.verify(row, payload, expected_payer=account.address)


@pytest.mark.parametrize("field,value", [("chainId", 1), ("verifyingContract", "0x" + "2" * 40),
                                       ("name", "Wrong Token"), ("version", "1")])
def test_wrong_domain_signatures_fail(prepared, field, value):
    row, account = prepared
    request = bindings.signing_request(row, payer=account.address)
    request["typed_data"]["domain"][field] = value
    signed = account.sign_message(encode_typed_data(full_message=request["typed_data"]))
    payload = {"authorization": request["authorization"], "signature": "0x" + signed.signature.hex()}
    with pytest.raises(bindings.VideoAuthorizationError):
        bindings.verify(row, payload, expected_payer=account.address)


def test_other_signer_personal_sign_and_other_quote_cannot_substitute(prepared):
    row, account = prepared
    request = bindings.signing_request(row, payer=account.address)
    signatures = ["0x" + Account.create().sign_message(encode_typed_data(full_message=request["typed_data"])).signature.hex(),
                  "0x" + account.sign_message(encode_defunct(text="not an EIP-712 payment")).signature.hex()]
    for signature in signatures:
        with pytest.raises(bindings.VideoAuthorizationError):
            bindings.verify(row, {"authorization": request["authorization"], "signature": signature},
                            expected_payer=account.address)
    other = copy.deepcopy(row["snapshot"])
    other["job_id"] = str(uuid4())
    other_row = quotes.PreparedVideoQuote(quotes._canonical(other)).values
    with pytest.raises(bindings.VideoAuthorizationError):
        bindings.verify(other_row, sign(row, account), expected_payer=account.address)
    with pytest.raises(bindings.VideoAuthorizationError):
        bindings.verify(row, sign(row, account), expected_payer=Account.create().address)


@pytest.mark.parametrize("kind", ["truncated", "compact", "erc6492", "zero", "high_s", "bad_v"])
def test_unsupported_or_malleable_signatures_are_private_errors(prepared, kind):
    row, account = prepared
    payload = sign(row, account)
    raw = bytes.fromhex(payload["signature"][2:])
    if kind in {"truncated", "compact"}:
        signature = raw[:-2 if kind == "truncated" else -1]
    elif kind == "erc6492":
        signature = raw + bytes.fromhex("6492" * 16)
    elif kind == "zero":
        signature = bytes(65)
    elif kind == "high_s":
        signature = raw[:32] + (SECPK1_N - int.from_bytes(raw[32:64], "big")).to_bytes(32, "big") + bytes([55 - raw[64]])
    else:
        signature = raw[:64] + b"\x00"
    payload["signature"] = "0x" + signature.hex()
    with pytest.raises(bindings.VideoAuthorizationError) as error:
        bindings.verify(row, payload, expected_payer=account.address)
    assert payload["signature"] not in str(error.value)


@pytest.mark.parametrize("mutation", ["extra", "missing", "not_dict", "not_auth", "auth_extra"])
def test_strict_inner_payload_shape_does_not_accept_unsigned_metadata(prepared, mutation):
    row, account = prepared
    payload = sign(row, account)
    if mutation == "extra":
        payload["resource"] = {"quote_id": str(row["id"])}
    elif mutation == "missing":
        payload.pop("signature")
    elif mutation == "not_dict":
        payload = []
    elif mutation == "not_auth":
        payload["authorization"] = []
    else:
        payload["authorization"]["quote_id"] = str(row["id"])
    with pytest.raises(bindings.VideoAuthorizationError):
        bindings.verify(row, payload, expected_payer=account.address)


def test_expiry_and_future_creation_reject_without_repricing(prepared, monkeypatch):
    row, account = prepared
    payload = sign(row, account)
    for timestamp in (row["snapshot"]["created"] - 1, row["snapshot"]["expires"]):
        monkeypatch.setattr(bindings, "_now", lambda: datetime.fromtimestamp(timestamp, UTC))
        with pytest.raises(bindings.VideoAuthorizationError):
            bindings.verify(row, payload, expected_payer=account.address)


def test_expiry_rechecked_after_cryptographic_work(prepared, monkeypatch):
    row, account = prepared
    payload = sign(row, account)
    clock = iter([row["snapshot"]["created"], row["snapshot"]["expires"]])
    monkeypatch.setattr(bindings, "_now", lambda: datetime.fromtimestamp(next(clock), UTC))
    with pytest.raises(bindings.VideoAuthorizationError):
        bindings.verify(row, payload, expected_payer=account.address)


def test_legacy_domainless_quotes_read_without_acquiring_signing_authority(prepared):
    row, account = prepared
    legacy = copy.deepcopy(row["snapshot"])
    legacy["schema"] = "aipg.video-quote-binding.v1"
    legacy["payment"].pop("eip712_domain")
    old_row = quotes.PreparedVideoQuote(quotes._canonical(legacy)).values
    assert quotes.view(old_row)["payment_enabled"] is False
    assert "eip712_domain" not in quotes.view(old_row)["payment"]
    with pytest.raises(bindings.VideoAuthorizationError):
        bindings.signing_request(old_row, payer=account.address)
    assert old_row["snapshot"] == legacy


def test_domain_corruption_and_self_payments_are_not_authorized(prepared):
    row, account = prepared
    changed = copy.deepcopy(row["snapshot"])
    changed["payment"]["eip712_domain"]["name"] = "Wrong Token"
    corrupt = quotes.PreparedVideoQuote(quotes._canonical(changed)).values
    with pytest.raises(bindings.VideoAuthorizationError):
        bindings.signing_request(corrupt, payer=account.address)
    with pytest.raises(bindings.VideoAuthorizationError):
        bindings.signing_request(row, payer=row["snapshot"]["payment"]["pay_to"])


def test_verification_reads_only_the_frozen_snapshot_and_mutable_views_do_not_change_it(prepared, monkeypatch):
    row, account = prepared
    payload = sign(row, account)
    original = copy.deepcopy(row)

    def forbidden(*_args, **_kwargs):
        pytest.fail("offline authorization must not reprice, reread ownership or dispatch")

    monkeypatch.setattr(pricing, "get_price", forbidden)
    monkeypatch.setattr(quotes, "new_session", forbidden)
    monkeypatch.setattr(videos, "get_available_models", forbidden)
    request = bindings.signing_request(row, payer=account.address)
    request["typed_data"]["types"]["TransferWithAuthorization"][0]["name"] = "changed"
    request["typed_data"]["domain"]["name"] = "changed"
    verified = bindings.verify(row, payload, expected_payer=account.address)
    assert verified.nonce == row["authorization_nonce"] and row == original


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["audio", "video"])
async def test_real_source_commitments_are_bound_to_the_authorization(prepared, admission, mp4, mode):
    row, account = prepared
    inputs = await videos.validate_video(plan_tests.request(mode, video=mp4), {}, async_submission=True)
    quote = quotes.prepare(account_id=uuid4(), idempotency_key=str(uuid4()), request_hash="a" * 64,
        plan=video_plans.compile_video(inputs), max_cost_micro=100_000, server_max_micro=100_000,
        network=row["snapshot"]["payment"]["network"], pay_to=row["snapshot"]["payment"]["pay_to"])
    payload = sign(quote.values, account)
    assert bindings.verify(quote.values, payload, expected_payer=account.address).job_id == str(quote.values["job_id"])
    changed = quote.snapshot
    changed["plan"]["inputs"]["sources"][0]["sha256"] = "b" * 64
    changed["plan_hash"] = quotes._hash(changed["plan"])
    substituted = quotes.PreparedVideoQuote(quotes._canonical(changed)).values
    with pytest.raises(bindings.VideoAuthorizationError):
        bindings.verify(substituted, payload, expected_payer=account.address)
