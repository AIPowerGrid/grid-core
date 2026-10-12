# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Scoped HTTP plus real SQL quotation; no facilitator, chain or GPU proof."""

import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa

from grid_api import database
from grid_api.routers import video_quotes as router
from grid_api.routers.tests import test_video_jobs as video_tests
from grid_api.services import accounts, credits, media, media_jobs, pricing, recipes, storage
from grid_api.services import video_quotes as quotes
from grid_api.services.tests import test_video_sources as source_tests
from grid_api.services.tests.test_media_job_journal import MODEL, OPENING, counts, owner
from grid_api.v2.schema import video_quotes

api = video_tests.api
db = video_tests.db
synthetic_dependencies = video_tests.synthetic_dependencies
mp4 = source_tests.mp4

def forbidden(*_args, **_kwargs):
    pytest.fail("video quotes must not upload, consume quota, reserve or dispatch")


@pytest_asyncio.fixture
async def quote_api(api, monkeypatch):
    client, aid, key, settings, quota, _available = api
    settings.video_x402_quotes_enabled = True
    settings.video_x402_quote_accounts = [aid]
    settings.video_x402_quotes_until = datetime.now(UTC) + timedelta(hours=1)
    settings.video_x402_quote_network = "eip155:84532"
    settings.video_x402_quote_pay_to = "0x" + "1" * 40
    settings.video_x402_quote_max_micro = 100_000
    settings.video_x402_quote_per_account = 8
    settings.video_x402_quote_capacity = 256
    monkeypatch.setattr(router, "get_settings", lambda: settings)
    monkeypatch.setattr(quota, "side_effect", forbidden)
    monkeypatch.setattr(storage, "upload_private_source", forbidden)
    monkeypatch.setattr(storage, "upload_source", forbidden)
    monkeypatch.setattr(media_jobs, "reserve", forbidden)
    monkeypatch.setattr(media.job_queue, "submit_job", forbidden)
    client._transport.app.include_router(router.router)
    yield api


async def post(api, *, key=None, body=None, credential=None):
    client, _aid, issued, *_ = api
    return await client.post(router.PATH, headers={"apikey": credential or issued,
        "Idempotency-Key": str(key or uuid4())}, json={"prompt": "A synthetic ball rolls", "model": MODEL,
        "max_cost_micro_usd": 100_000, **(body or {})})


async def rows():
    async with await database.new_session() as session:
        return await session.scalar(sa.select(sa.func.count()).select_from(video_quotes))


@pytest.mark.asyncio
async def test_quote_replay_freezes_one_random_seed_price_and_graph_without_financial_effects(quote_api, monkeypatch):
    client, aid, credential, settings, quota, available = quote_api
    key = uuid4()
    random = []
    monkeypatch.setattr(media.secrets, "randbelow", lambda limit: random.append(limit) or 42)
    first = await post(quote_api, key=key)
    assert first.status_code == 201, first.text
    data = first.json()
    assert data["seed"] == 42 and data["payment_enabled"] is False
    assert data["payment"]["amount_micro"] == 67_500
    assert data["price"]["discount_bps"] == 0
    assert first.headers["cache-control"] == "no-store"
    assert "payment-required" not in first.headers
    assert await credits.get_balance(aid) == OPENING and await counts() == (0, 0, 0)
    settings.video_x402_quotes_enabled = False
    settings.video_x402_quote_network = "eip155:8453"
    settings.video_x402_quote_pay_to = "0x" + "2" * 40
    recipes._BY_MODEL.clear()
    monkeypatch.setitem(pricing.PRICING, MODEL.lower(), pricing.ModelPrice(0, 0, video_per_second=99))
    available.reset_mock()
    second = await post(quote_api, key=key)
    assert second.status_code == 200 and second.json() == data
    assert len(random) == 1 and await rows() == 1
    for url, headers in ((first.headers["location"], {"apikey": credential}),
                         (router.PATH, {"apikey": credential, "Idempotency-Key": str(key)})):
        recovered = await client.get(url, headers=headers)
        assert recovered.status_code == 200 and recovered.json() == data
        assert recovered.headers["cache-control"] == "no-store"
    available.assert_not_awaited()
    quota.assert_not_awaited()
    assert await credits.get_balance(aid) == OPENING and await counts() == (0, 0, 0)
    assert (await client.post(first.headers["location"] + "/pay", json={})).status_code == 404


@pytest.mark.asyncio
async def test_quote_and_credit_pilot_cohorts_are_independent_without_execution_authority(quote_api):
    client, aid, credential, settings, quota, _available = quote_api
    settings.media_async_enabled = True
    settings.media_async_admission_mode = "pilot"
    settings.media_async_canary_accounts = [uuid4()]
    settings.media_async_canary_until = datetime.now(UTC) + timedelta(minutes=5)
    first = await post(quote_api)
    assert first.status_code == 201 and not first.json()["payment_enabled"]
    credit = await video_tests.post(quote_api)
    assert credit.status_code == 503
    assert await rows() == 1 and await credits.get_balance(aid) == OPENING and await counts() == (0, 0, 0)
    quota.assert_not_awaited()
    assert (await client.get(first.headers["location"], headers={"apikey": credential})).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["audio", "video"])
async def test_inline_audio_video_are_validated_committed_not_uploaded_and_changed_bytes_conflict(quote_api, mp4, mode):
    key = uuid4()
    audio = base64.b64encode(source_tests.wav()).decode("ascii")
    body = {"frames": 9, "size": "256x256", "reference_audio": audio}
    if mode == "video":
        body["reference_video"] = base64.b64encode(mp4).decode("ascii")
    first = await post(quote_api, key=key, body=body)
    assert first.status_code == 201, first.text
    assert first.json()["payment"]["amount_micro"] == 7500
    assert len(first.json()["inputs"]["sources"]) == (2 if mode == "video" else 1)
    assert "reference_audio" not in first.json() and "reference_video" not in first.json()
    assert audio not in str(first.json())
    equivalent = await post(quote_api, key=key, body={**body, "reference_audio": "data:audio/wav;base64," + audio})
    assert equivalent.status_code == 200 and equivalent.json() == first.json()
    changed = bytearray(source_tests.wav())
    changed[-1] = 1
    conflict = await post(quote_api, key=key, body={**body, "reference_audio": base64.b64encode(changed).decode("ascii")})
    assert conflict.status_code == 409 and await rows() == 1 and await counts() == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"prompt": "different"}, {"seed": 2}, {"max_cost_micro_usd": 90_000}, {"frames": 89}])
async def test_quote_changed_intent_is_conflict_without_side_effects(quote_api, change):
    key = uuid4()
    assert (await post(quote_api, key=key)).status_code == 201
    response = await post(quote_api, key=key, body=change)
    assert response.status_code == 409 and response.headers["cache-control"] == "no-store"
    assert await rows() == 1 and await counts() == (0, 0, 0)


@pytest.mark.asyncio
async def test_quote_read_is_authenticated_owned_and_scoped_after_rollback(quote_api):
    client, aid, credential, settings, *_ = quote_api
    key = uuid4()
    first = await post(quote_api, key=key)
    settings.video_x402_quotes_enabled = False
    observer = await accounts.issue_key(aid, scopes=["account.read"])
    stranger = await accounts.issue_key(await owner(0), scopes=["inference.submit"])
    for endpoint, selector in ((first.headers["location"], {}), (router.PATH, {"Idempotency-Key": str(key)})):
        for auth_key, expected in ((None, 401), (observer, 403), (stranger, 404), (credential, 200)):
            headers = {**selector, **({"apikey": auth_key} if auth_key else {})}
            response = await client.get(endpoint, headers=headers)
            assert response.status_code == expected
            assert response.headers["cache-control"] == "no-store"
    assert (await post(quote_api, credential=observer)).status_code == 403


@pytest.mark.asyncio
async def test_lost_commit_reply_uses_get_only_no_new_seed_or_quote(quote_api, monkeypatch):
    client, _aid, credential, settings, *_ = quote_api
    original = quotes.persist
    committed = []

    async def lose_reply(*args, **kwargs):
        committed.append(await original(*args, **kwargs))
        raise ConnectionError("synthetic lost commit response")

    monkeypatch.setattr(quotes, "persist", lose_reply)
    key = uuid4()
    first = await post(quote_api, key=key)
    assert first.status_code == 503 and len(committed) == 1
    settings.video_x402_quotes_enabled = False
    monkeypatch.setattr(router, "validate_video", forbidden)
    recovered = await client.get(router.PATH, headers={"apikey": credential, "Idempotency-Key": str(key)})
    assert recovered.status_code == 200 and recovered.json() == quotes.view(committed[0][0])
    assert await rows() == 1 and await counts() == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["disabled", "foreign", "expired", "capacity"])
async def test_private_admission_and_caps_fail_before_source_decode(quote_api, monkeypatch, gate):
    settings = quote_api[3]
    if gate == "capacity":
        settings.video_x402_quote_per_account = 1
        assert (await post(quote_api)).status_code == 201
    elif gate == "disabled":
        settings.video_x402_quotes_enabled = False
    elif gate == "foreign":
        settings.video_x402_quote_accounts = [uuid4()]
    else:
        settings.video_x402_quotes_until = datetime.now(UTC) - timedelta(seconds=1)
    monkeypatch.setattr(media, "decode_video_keyframes", forbidden)
    monkeypatch.setattr(router, "validate_video", forbidden)
    response = await post(quote_api, body={"image": "synthetic-unparsed-input"})
    assert response.status_code == (429 if gate == "capacity" else 503)
    assert await counts() == (0, 0, 0)


@pytest.mark.asyncio
async def test_pilot_expiry_rechecked_after_source_preparation(quote_api, monkeypatch):
    settings = quote_api[3]
    original = router.validate_video

    async def expire_after_validation(*args, **kwargs):
        inputs = await original(*args, **kwargs)
        settings.video_x402_quotes_until = datetime.now(UTC) - timedelta(seconds=1)
        return inputs

    monkeypatch.setattr(router, "validate_video", expire_after_validation)
    assert (await post(quote_api)).status_code == 503
    assert await rows() == 0 and await counts() == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"max_cost_micro_usd": 1}, {"model": "unknown synthetic model"}])
async def test_unpriced_or_over_ceiling_cannot_quote(quote_api, change):
    response = await post(quote_api, body=change)
    assert response.status_code in {422, 503}
    assert await rows() == 0 and await counts() == (0, 0, 0)


@pytest.mark.asyncio
async def test_quote_expiry_preserves_binding_without_implicit_requote(quote_api, monkeypatch):
    key = uuid4()
    first = await post(quote_api, key=key)
    monkeypatch.setattr(quotes, "_now", lambda: datetime.fromtimestamp(first.json()["expires"], UTC))
    repeat = await post(quote_api, key=key)
    assert repeat.status_code == 200 and repeat.json() == {**first.json(), "state": "expired"}
    assert await rows() == 1


@pytest.mark.asyncio
async def test_input_errors_do_not_echo_secret_or_accept_client_graph(quote_api):
    response = await post(quote_api, body={"recipe_spec": {"secret": "private-marker"}})
    assert response.status_code == 422 and "private-marker" not in response.text
    client, _, credential, *_ = quote_api
    response = await client.get(router.PATH, headers={"apikey": credential, "Idempotency-Key": "private-marker"})
    assert response.status_code == 422 and "private-marker" not in response.text
    assert await rows() == 0
