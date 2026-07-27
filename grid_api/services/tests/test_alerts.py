# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging

import httpx
import pytest

from grid_api import redis_client
from grid_api.services import alerts


def test_payload_redacts_and_excludes_sensitive_fields():
    payload = alerts.build_payload(
        "test",
        "critical",
        "contact person@example.com using grid-secret-value-1234567890",
        {
            "account": "safe-correlation-id",
            "api_key": "must-not-appear",
            "prompt": "must-not-appear",
            "note": "mail me@example.com https://discord.com/api/webhooks/1/secret",
        },
    )
    rendered = str(payload)
    assert "person@example.com" not in rendered
    assert "me@example.com" not in rendered
    assert "must-not-appear" not in rendered
    assert "/api/webhooks/1/secret" not in rendered
    assert "safe-correlation-id" in rendered
    assert payload["allowed_mentions"] == {"parse": []}


def test_only_official_discord_webhooks_are_accepted():
    assert alerts._valid_webhook("https://discord.com/api/webhooks/123/abc")
    assert not alerts._valid_webhook("http://discord.com/api/webhooks/123/abc")
    assert not alerts._valid_webhook("https://example.com/api/webhooks/123/abc")
    assert not alerts._valid_webhook("https://discord.com/channels/123")


def test_transport_request_logs_are_suppressed():
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    old_httpx = httpx_logger.level
    old_httpcore = httpcore_logger.level
    try:
        httpx_logger.setLevel("INFO")
        httpcore_logger.setLevel("INFO")
        alerts._harden_transport_logging()
        assert httpx_logger.level >= logging.WARNING
        assert httpcore_logger.level >= logging.WARNING
    finally:
        httpx_logger.setLevel(old_httpx)
        httpcore_logger.setLevel(old_httpcore)


@pytest.mark.asyncio
async def test_delivery_posts_redacted_payload_without_leaking_url(monkeypatch):
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    old_client = alerts._client
    alerts._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        payload = alerts.build_payload("signup", "success", "new account", {"account": "abc"})
        ok = await alerts._deliver("https://discord.com/api/webhooks/123/redacted-test", payload)
    finally:
        await alerts._client.aclose()
        alerts._client = old_client
    assert ok is True
    assert len(seen) == 1
    assert seen[0].headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_distributed_dedupe_claims_once_and_releases_failed_delivery(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.values = {}

        async def set(self, key, value, *, ex, nx):
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

        async def eval(self, _script, _keys, key, token):
            if self.values.get(key) == token:
                del self.values[key]
                return 1
            return 0

    fake = FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    first = await alerts._claim_distributed("same-event", 60)
    assert first
    assert await alerts._claim_distributed("same-event", 60) is None
    await alerts._release_distributed(first)
    assert await alerts._claim_distributed("same-event", 60)
