# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from types import SimpleNamespace

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from grid_api.services import audio, worker_identity


class FakeRedis:
    def __init__(self):
        self.values = {}

    async def set(self, key, value, *, nx=False, ex=None):
        assert nx is True
        assert ex == 600
        if key in self.values:
            return False
        self.values[key] = value
        return True


def _profile():
    return {
        "id": "ace-step-v1.5-xl-turbo",
        "version": "0.1.0",
        "digest": "a" * 64,
        "signing_key_id": "release-2026-01",
        "capability_tier": "audio.ace-step.standard",
        "runtime_adapter": audio.ACE_STEP_RUNTIME_ADAPTER,
        "runtime_digest": audio.ACE_STEP_RUNTIME_DIGEST,
        "recipe_root": audio.ACE_STEP_RECIPE_ROOT,
        "canary_completed_at": "2026-07-15T00:00:00+00:00",
        "canary_elapsed_seconds": 8.5,
    }


def _proof(*, now=1_800_000_100, worker_name="audio-rig", profile=None):
    wallet = Account.from_key("0x" + "11" * 32)
    signer = Account.from_key("0x" + "22" * 32)
    delegation = {
        "version": 1,
        "chain_id": 8453,
        "audience": "api.aipowergrid.io",
        "delegation_id": "ab" * 16,
        "payout_wallet": wallet.address.lower(),
        "worker_signer": signer.address.lower(),
        "worker_name": worker_name,
        "issued_at": 1_800_000_000,
        "expires_at": 1_800_000_000 + 90 * 86400,
    }
    certificate = {
        "payload": delegation,
        "signature": Account.sign_message(encode_defunct(text=worker_identity.delegation_message(delegation)), wallet.key).signature.hex(),
    }
    payload = {
        "version": 1,
        "timestamp": now,
        "nonce": "cd" * 16,
        "worker_signer": signer.address.lower(),
        "worker_name": worker_name,
        "models": ["ace-step-v1.5-xl-turbo"],
        "job_types": ["audio"],
        "bridge_agent": "comfy-bridge/ws:1",
        "profile_digest": profile["digest"] if profile else None,
        "profile_recipe_root": profile["recipe_root"] if profile else None,
    }
    proof = {
        "payload": payload,
        "signature": Account.sign_message(encode_defunct(text=worker_identity.registration_message(payload)), signer.key).signature.hex(),
        "delegation": certificate,
    }
    return proof, wallet, signer


@pytest.fixture
def identity_env(monkeypatch):
    redis = FakeRedis()
    settings = SimpleNamespace(
        worker_identity_chain_id=8453,
        worker_identity_audience="api.aipowergrid.io",
        worker_registration_skew_seconds=300,
        approved_worker_profile_digests="a" * 64,
    )
    monkeypatch.setattr(worker_identity, "get_redis", lambda: redis)
    monkeypatch.setattr(worker_identity, "get_settings", lambda: settings)
    return redis


async def _verify(proof, wallet, *, profile=None, required=True, now=1_800_000_100):
    return await worker_identity.verify_registration(
        proof=proof,
        payout_wallet=wallet.address,
        worker_name="audio-rig",
        models=["ace-step-v1.5-xl-turbo"],
        job_types=["audio"],
        bridge_agent="comfy-bridge/ws:1",
        worker_profile=profile,
        required=required,
        now=now,
    )


@pytest.mark.asyncio
async def test_wallet_delegation_and_worker_proof_are_both_verified(identity_env):
    profile = _profile()
    proof, wallet, signer = _proof(profile=profile)
    verified = await _verify(proof, wallet, profile=profile)
    assert verified.signer_address == signer.address.lower()
    assert verified.payout_wallet == wallet.address.lower()
    assert verified.delegation_id == "ab" * 16


@pytest.mark.asyncio
async def test_registration_nonce_is_one_use(identity_env):
    proof, wallet, _signer = _proof()
    await _verify(proof, wallet)
    with pytest.raises(worker_identity.WorkerIdentityError, match="already used"):
        await _verify(proof, wallet)


@pytest.mark.asyncio
async def test_registration_rejects_account_wallet_mismatch(identity_env):
    proof, _wallet, _signer = _proof()
    other_wallet = Account.from_key("0x" + "33" * 32)
    with pytest.raises(worker_identity.WorkerIdentityError, match="authenticated account"):
        await _verify(proof, other_wallet)


@pytest.mark.asyncio
async def test_registration_rejects_stale_proof(identity_env):
    proof, wallet, _signer = _proof(now=1_800_000_100)
    with pytest.raises(worker_identity.WorkerIdentityError, match="stale"):
        await _verify(proof, wallet, now=1_800_000_401)


@pytest.mark.asyncio
async def test_registration_rejects_advertisement_tampering(identity_env):
    proof, wallet, _signer = _proof()
    proof["payload"]["models"] = ["more-expensive-model"]
    with pytest.raises(worker_identity.WorkerIdentityError, match="advertised capabilities"):
        await _verify(proof, wallet)


@pytest.mark.asyncio
async def test_missing_proof_is_optional_only_for_legacy_workers(identity_env):
    assert (
        await worker_identity.verify_registration(
            proof=None,
            payout_wallet="",
            worker_name="legacy",
            models=["legacy-model"],
            job_types=["image"],
            bridge_agent="legacy",
            worker_profile=None,
            required=False,
        )
        is None
    )
    with pytest.raises(worker_identity.WorkerIdentityError, match="required"):
        await worker_identity.verify_registration(
            proof=None,
            payout_wallet="",
            worker_name="managed",
            models=["ace-step-v1.5-xl-turbo"],
            job_types=["audio"],
            bridge_agent="comfy-bridge/ws:1",
            worker_profile=_profile(),
            required=True,
        )


def test_worker_profile_rejects_private_hardware_fields():
    profile = {**_profile(), "gpu": "RTX 5090"}
    with pytest.raises(worker_identity.WorkerIdentityError, match="unsupported"):
        worker_identity.normalize_worker_profile(profile)


def test_worker_profile_digest_requires_canonical_lowercase_hex():
    for digest in ("A" * 64, "a" * 62 + "  "):
        with pytest.raises(worker_identity.WorkerIdentityError, match="digest"):
            worker_identity.normalize_worker_profile({**_profile(), "digest": digest})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "another-audio-model"),
        ("runtime_adapter", "another-adapter"),
        ("runtime_digest", "f" * 64),
        ("recipe_root", "e" * 64),
        ("capability_tier", "audio.ace-step.unbounded"),
    ],
)
def test_worker_profile_must_match_core_owned_audio_contract(field, value):
    profile = {**_profile(), field: value}
    with pytest.raises(worker_identity.WorkerIdentityError, match=field):
        worker_identity.normalize_worker_profile(profile)


@pytest.mark.asyncio
async def test_managed_profile_must_be_core_approved(identity_env, monkeypatch):
    profile = _profile()
    proof, wallet, _signer = _proof(profile=profile)
    settings = worker_identity.get_settings()
    monkeypatch.setattr(settings, "approved_worker_profile_digests", "")
    with pytest.raises(worker_identity.WorkerIdentityError, match="not approved"):
        await _verify(proof, wallet, profile=profile)
