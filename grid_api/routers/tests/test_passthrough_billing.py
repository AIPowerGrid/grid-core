# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Grid-side metering for the raw passthrough endpoints (/v1/responses, /v1/messages).

Proves the auditor's P0 — these formats are now BILLED on grid-counted tokens
(prompt flattened from the request, completion from the text the grid relayed /
assembled), never on worker/backend-reported `usage`. Drives the shared
`_passthrough` plumbing directly with a faked `token_stream`.
"""

import json
import uuid

import pytest
from fastapi import HTTPException

from grid_api.routers import _passthrough as pt
from grid_api.services import credits, den, token_stream


def _fake_subscribe(events):
    async def gen(job_id, *a, **kw):
        for e in events:
            yield e
    return gen


def dry_observe(monkeypatch):
    """Dry-run: the collectors OBSERVE via charge_request (LIVE settlement is in
    worker_ws, tested in test_reservation_lifecycle.py). Record the would-charge."""
    monkeypatch.setattr(credits, "CHARGING_ENABLED", False)
    charge = []

    async def fake_charge(user, model, p, c, job_id):
        charge.append({"p": p, "c": c})
        return {"status": "dry_run", "charged": 0}

    monkeypatch.setattr(credits, "charge_request", fake_charge)
    return charge


# ── pure extractors ──────────────────────────────────────────────────────────


def test_extract_prompt_text_anthropic():
    req = {
        "system": "You are helpful.",
        "messages": [
            {"role": "user", "content": "What is the capital of France?"},
            {"role": "assistant", "content": [{"type": "text", "text": "Paris."}]},
            {"role": "user", "content": [
                {"type": "tool_result", "content": "tool said hi"},
                {"type": "text", "text": "thanks"},
            ]},
        ],
        "tools": [{"name": "lookup", "description": "look things up", "input_schema": {"type": "object"}}],
    }
    txt = pt.extract_prompt_text("anthropic", req)
    for needle in ["You are helpful.", "capital of France", "Paris.", "tool said hi", "thanks", "lookup"]:
        assert needle in txt


def test_extract_prompt_text_responses():
    req = {
        "instructions": "Be terse.",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "ping"}]},
            {"type": "function_call_output", "output": "pong-result"},
        ],
    }
    txt = pt.extract_prompt_text("openai-responses", req)
    assert "Be terse." in txt and "ping" in txt and "pong-result" in txt


def test_extract_output_text_anthropic():
    full = {"content": [
        {"type": "text", "text": "Hello there"},
        {"type": "tool_use", "input": {"q": "weather"}},
    ], "usage": {"output_tokens": 0}}
    out = pt.extract_output_text("anthropic", full)
    assert "Hello there" in out and "weather" in out


def test_extract_output_text_responses_output_text():
    full = {"output_text": "the answer is 42", "usage": {"output_tokens": 0}}
    assert pt.extract_output_text("openai-responses", full) == "the answer is 42"


def test_stream_delta_text_both_shapes():
    anth = json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi "}})
    resp = json.dumps({"type": "response.output_text.delta", "delta": "world"})
    ping = json.dumps({"type": "ping"})
    assert pt._stream_delta_text(anth) == "Hi "
    assert pt._stream_delta_text(resp) == "world"
    assert pt._stream_delta_text(ping) == ""
    assert pt._stream_delta_text("not json") == ""


def test_normalize_output_budget_updates_raw_request():
    anth = {}
    assert pt.normalize_output_budget("anthropic", anth) == pt.DEFAULT_OUTPUT_TOKENS
    assert anth["max_tokens"] == pt.DEFAULT_OUTPUT_TOKENS

    resp = {"max_tokens": 123}
    assert pt.normalize_output_budget("openai-responses", resp) == 123
    assert resp["max_output_tokens"] == 123

    with pytest.raises(HTTPException):
        pt.normalize_output_budget("openai-responses", {"max_output_tokens": 0})


# ── completion-token helper (what worker_ws bills on) ────────────────────────


def test_completion_tokens_prefers_full_json():
    full = {"content": [{"type": "text", "text": "the assembled answer body"}]}
    assert pt.completion_tokens("anthropic", ["ignored"], full) == den.count_tokens("the assembled answer body")


def test_completion_tokens_falls_back_to_stream_deltas():
    deltas = [json.dumps({"delta": {"type": "text_delta", "text": "a "}}),
              json.dumps({"delta": {"type": "text_delta", "text": "b c"}})]
    assert pt.completion_tokens("anthropic", deltas, None) == den.count_tokens("a b c")


# ── collect/stream: dry-run OBSERVE on grid counts; never settle in collector ──


@pytest.mark.asyncio
async def test_collect_observes_grid_output_in_dry_run(monkeypatch):
    charge = dry_observe(monkeypatch)
    answer = "Paris is the capital of France, a well known fact."
    full = {"content": [{"type": "text", "text": answer}],
            "usage": {"input_tokens": 0, "output_tokens": 0}}  # worker LIES
    events = [{"text": token_stream.DONE_SENTINEL, "full_json": full}]
    monkeypatch.setattr(pt.token_stream, "subscribe_tokens", _fake_subscribe(events))

    out = await pt.collect_passthrough(
        "j1", api_format="anthropic", user={"account_id": uuid.uuid4()}, model="claude-x", prompt_toks=11)
    assert out == full
    assert len(charge) == 1 and charge[0]["p"] == 11
    assert charge[0]["c"] == den.count_tokens(answer) > 0


@pytest.mark.asyncio
async def test_collect_does_not_settle_in_live(monkeypatch):
    """LIVE: the collector neither charges nor reconciles — worker_ws settles."""
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    charged = []
    monkeypatch.setattr(credits, "charge_request",
                        lambda *a, **k: charged.append(a) or {"status": "ok"})
    full = {"content": [{"type": "text", "text": "hello"}]}
    events = [{"text": token_stream.DONE_SENTINEL, "full_json": full}]
    monkeypatch.setattr(pt.token_stream, "subscribe_tokens", _fake_subscribe(events))
    out = await pt.collect_passthrough("j2", api_format="anthropic",
                                       user={"account_id": uuid.uuid4()}, model="m", prompt_toks=7)
    assert out == full and not charged


@pytest.mark.asyncio
async def test_collect_error_raises_without_charging(monkeypatch):
    from fastapi import HTTPException
    charge = dry_observe(monkeypatch)
    events = [{"text": token_stream.DONE_SENTINEL, "error": "backend down", "code": 502}]
    monkeypatch.setattr(pt.token_stream, "subscribe_tokens", _fake_subscribe(events))
    with pytest.raises(HTTPException):
        await pt.collect_passthrough("j3", api_format="anthropic",
                                     user={"account_id": uuid.uuid4()}, model="m", prompt_toks=7)
    assert not charge  # error path doesn't observe a charge


@pytest.mark.asyncio
async def test_stream_observes_relayed_deltas_in_dry_run(monkeypatch):
    charge = dry_observe(monkeypatch)
    deltas = ["The grid ", "meters in ", "tokens."]
    events = [{"raw": True, "event": "content_block_delta",
               "data": json.dumps({"delta": {"type": "text_delta", "text": d}})} for d in deltas]
    events.append({"text": token_stream.DONE_SENTINEL})
    monkeypatch.setattr(pt.token_stream, "subscribe_tokens", _fake_subscribe(events))

    async for _ in pt.stream_passthrough("j4", api_format="anthropic",
                                         user={"account_id": uuid.uuid4()}, model="m", prompt_toks=4):
        pass
    assert len(charge) == 1
    assert charge[0]["c"] == den.count_tokens("".join(deltas)) > 0


@pytest.mark.asyncio
async def test_stream_observes_once_on_disconnect(monkeypatch):
    charge = dry_observe(monkeypatch)
    events = [
        {"raw": True, "event": "x", "data": json.dumps({"delta": "first piece "})},
        {"raw": True, "event": "x", "data": json.dumps({"delta": "second piece"})},
    ]
    monkeypatch.setattr(pt.token_stream, "subscribe_tokens", _fake_subscribe(events))

    agen = pt.stream_passthrough("j5", api_format="openai-responses",
                                 user={"account_id": uuid.uuid4()}, model="m", prompt_toks=2)
    await agen.__anext__()  # first relayed event → "first piece "
    await agen.aclose()     # client disconnects
    assert len(charge) == 1
    assert charge[0]["c"] == den.count_tokens("first piece ")


@pytest.mark.asyncio
async def test_authorize_passthrough_dry_run_is_noop(monkeypatch):
    monkeypatch.setattr(credits, "CHARGING_ENABLED", False)
    auth = await pt.authorize_passthrough(
        {"account_id": uuid.uuid4()}, "m", "anthropic",
        {"messages": [{"role": "user", "content": "hi there"}]}, 100, "j5",
    )
    assert auth["ok"] and auth["reserved"] == 0 and auth["status"] == "dry_run"
    assert auth["prompt_toks"] > 0  # grid still counts the prompt
