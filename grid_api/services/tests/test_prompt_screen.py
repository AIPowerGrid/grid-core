# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import json
import logging

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr

from grid_api.config import GridSettings
from grid_api.services import alerts, prompt_screen

SECRET = "sk-test-screen-key-000000000000"
BANNED_MARK = "prompt-text-that-must-never-be-logged"


def _settings(**changes):
    values = {"prompt_screen_mode": "shadow", "prompt_screen_api_key": SecretStr(SECRET)}
    values.update(changes)
    return GridSettings(_env_file=None, **values)


@pytest_asyncio.fixture
async def screen(monkeypatch):
    """Started screen whose transport is a recording mock."""
    state = {"requests": [], "score": 0.01, "status": 200, "body": None, "alerts": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        if state["body"] is not None:
            return httpx.Response(state["status"], content=state["body"])
        return httpx.Response(state["status"], json={
            "results": [{"flagged": True, "category_scores": {"sexual/minors": state["score"], "sexual": 0.99}}],
        })

    async def start(**changes):
        settings = _settings(**changes)
        monkeypatch.setattr(prompt_screen, "get_settings", lambda: settings)
        await prompt_screen.start()
        if prompt_screen._client is not None:
            headers = dict(prompt_screen._client.headers)
            await prompt_screen._client.aclose()
            prompt_screen._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=headers)

    monkeypatch.setattr(alerts, "emit", lambda kind, severity, summary, **kw: state["alerts"].append((kind, kw)) or True)
    monkeypatch.setattr(prompt_screen, "_stats", {"scored": 0, "over": 0, "failed": 0, "dropped": 0})
    state["start"] = start
    yield state
    await prompt_screen.stop()


@pytest.mark.parametrize("url,ok", [
    ("https://api.openai.com/v1/moderations", True),
    ("http://10.0.0.5:11434/v1/moderations", True),
    ("http://127.0.0.1:8000/x", True),
    ("http://api.openai.com/v1/moderations", False),
    ("http://8.8.8.8/x", False),
    ("https://user:pass@example.com/x", False),
    ("ftp://example.com/x", False),
    ("", False),
])
def test_endpoint_rules(url, ok):
    assert prompt_screen.valid_endpoint(url) is ok


def test_media_text_takes_prompts_and_ignores_negative_prompt():
    payload = {"prompt": "a lighthouse", "negative_prompt": "child", "n": 2,
               "recipe_inputs": {"prompt": "a lighthouse", "local_prompts": "scene two", "timeline": [{"x": 1}]}}
    assert prompt_screen.media_text(payload) == "a lighthouse\nscene two"
    assert prompt_screen.media_text({"prompt": 5}) == ""
    assert prompt_screen.media_text(None) == ""


def test_default_is_off_and_settings_reject_unknown_mode():
    assert GridSettings(_env_file=None).prompt_screen_mode == "off"
    with pytest.raises(ValueError):
        GridSettings(_env_file=None, prompt_screen_mode="enforce")
    assert SECRET not in repr(_settings())


@pytest.mark.asyncio
async def test_off_mode_is_inert(screen):
    await screen["start"](prompt_screen_mode="off")
    assert prompt_screen.observe("image", "m", "hello", "acct") is False
    assert prompt_screen._worker_task is None and screen["requests"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"prompt_screen_api_key": None},
    {"prompt_screen_api_key": SecretStr("  ")},
    {"prompt_screen_url": "http://example.com/v1/moderations"},
])
async def test_misconfiguration_disables_instead_of_failing_startup(screen, changes):
    await screen["start"](**changes)
    assert prompt_screen._worker_task is None
    assert prompt_screen.observe("image", "m", "hello", "acct") is False


@pytest.mark.asyncio
async def test_scores_in_background_without_leaking_text_or_key(screen, caplog):
    await screen["start"]()
    caplog.set_level(logging.DEBUG)
    assert prompt_screen.observe("openai-chat", "model-a", f"hello {BANNED_MARK} ghp_" + "a" * 36, "acct-1") is True
    await prompt_screen.flush()
    request = screen["requests"][0]
    body = json.loads(request.content)
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    assert body["model"] == "omni-moderation-latest"
    assert BANNED_MARK in body["input"]
    assert "ghp_" + "a" * 36 not in body["input"]  # credentials are redacted before leaving
    assert prompt_screen.stats()["scored"] == 1 and prompt_screen.stats()["over"] == 0
    assert screen["alerts"] == []
    logged = caplog.text
    assert "score=0.010000 over=0" in logged
    assert BANNED_MARK not in logged and SECRET not in logged and "acct-1" not in logged


@pytest.mark.asyncio
async def test_only_the_configured_category_decides_and_hits_alert(screen, caplog):
    await screen["start"]()
    caplog.set_level(logging.INFO)
    # The provider's overall "flagged" and the adult "sexual" score are ignored.
    prompt_screen.observe("image", "m", "adult content", "acct-1")
    await prompt_screen.flush()
    assert prompt_screen.stats()["over"] == 0 and screen["alerts"] == []
    screen["score"] = 0.91
    prompt_screen.observe("image", "m", BANNED_MARK, "acct-1")
    await prompt_screen.flush()
    assert prompt_screen.stats()["over"] == 1
    kind, kw = screen["alerts"][0]
    assert kind == "safety_screen_hit"
    assert kw["fields"]["score"] == "0.9100" and kw["fields"]["path"] == "image"
    assert BANNED_MARK not in str(kw) and "acct-1" not in str(kw)
    assert BANNED_MARK not in caplog.text


@pytest.mark.asyncio
async def test_threshold_is_inclusive_and_configurable(screen):
    await screen["start"](prompt_screen_threshold=0.2)
    screen["score"] = 0.2
    prompt_screen.observe("video", "m", "x", None)
    await prompt_screen.flush()
    assert prompt_screen.stats()["over"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [
    (500, b"upstream exploded"),
    (401, b'{"error":{"message":"bad key"}}'),
    (200, b"not json"),
    (200, b'{"results":[]}'),
    (200, b'{"results":[{"category_scores":{}}]}'),
    (200, b'{"results":[{"category_scores":{"sexual/minors":"0.9"}}]}'),
    (200, b'{"results":[{"category_scores":{"sexual/minors":true}}]}'),
    (200, b'{"results":[{"category_scores":{"sexual/minors":7}}]}'),
])
async def test_failures_are_counted_and_never_treated_as_a_score(screen, caplog, status, body):
    await screen["start"]()
    screen["status"], screen["body"] = status, body
    prompt_screen.observe("anthropic", "m", BANNED_MARK, "acct")
    await prompt_screen.flush()
    stats = prompt_screen.stats()
    assert stats["failed"] == 1 and stats["scored"] == 0 and stats["over"] == 0
    assert screen["alerts"][0][0] == "safety_screen_unavailable"
    assert BANNED_MARK not in caplog.text and "upstream exploded" not in caplog.text
    # The worker survives and scores the next item.
    screen["status"], screen["body"] = 200, None
    prompt_screen.observe("anthropic", "m", "next", "acct")
    await prompt_screen.flush()
    assert prompt_screen.stats()["scored"] == 1


@pytest.mark.asyncio
async def test_observe_never_raises_or_blocks_and_keeps_the_tail(screen):
    await screen["start"](prompt_screen_max_chars=500, prompt_screen_queue_size=10)
    assert prompt_screen.observe("image", "m", "", "a") is False
    assert prompt_screen.observe("image", "m", None, "a") is False
    assert prompt_screen.observe("image", "m", 123, "a") is False
    prompt_screen.observe("image", "m", "old " * 500 + "NEWEST", "a")
    await prompt_screen.flush()
    sent = json.loads(screen["requests"][0].content)["input"]
    assert len(sent) == 500 and sent.endswith("NEWEST")
    # A stalled worker fills the queue; further observations are dropped, not awaited.
    prompt_screen._worker_task.cancel()
    await asyncio.gather(prompt_screen._worker_task, return_exceptions=True)
    assert prompt_screen.observe("image", "m", "x", "a") is False


@pytest.mark.asyncio
async def test_full_queue_drops(screen, monkeypatch):
    await screen["start"](prompt_screen_queue_size=10)
    gate = asyncio.Event()

    async def slow(text):
        await gate.wait()
        return 0.0

    monkeypatch.setattr(prompt_screen, "score", slow)
    results = [prompt_screen.observe("image", "m", f"p{i}", "a") for i in range(14)]
    await asyncio.sleep(0)
    assert results.count(False) >= 3 and prompt_screen.stats()["dropped"] >= 3
    gate.set()
    await prompt_screen.flush()


@pytest.mark.asyncio
async def test_media_submission_observes_after_admission_and_survives_screen_errors(monkeypatch):
    from grid_api.services import media

    seen = []

    async def prepare(model, job_type, payload):
        return payload, None

    class Stop(Exception):
        pass

    def stop(*args, **kwargs):
        raise Stop

    monkeypatch.setattr(media, "prepare_dispatch", prepare)
    monkeypatch.setattr(media, "uuid4", stop)  # halt right after the observation point
    monkeypatch.setattr(prompt_screen, "observe", lambda *a: seen.append(a) or True)
    with pytest.raises(Stop):
        await media.submit_and_wait("img-model", "image", {"prompt": "a lighthouse", "negative_prompt": "blur"}, 1,
                                    account_id="acct")
    assert seen == [("image", "img-model", "a lighthouse", "acct")]
