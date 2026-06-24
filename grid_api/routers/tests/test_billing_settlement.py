# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Router-level money tests for the second billing pass (audit re-review).

These prove the invariants the auditor flagged as still-broken in the first pass:

  * Settlement bills on GRID-counted tokens, NEVER worker-reported `usage`
    (a lying/silent worker can't zero or inflate the bill).
  * A client disconnect mid-stream still settles in the `finally` — the held
    reservation is reconciled, not stranded.
  * A worker error still settles (so the reserve is released, not stranded).

They drive `_stream_openai` / `_collect_response` directly with a faked
`token_stream.subscribe_tokens`, capturing what reaches `credits.reconcile`.
"""

import uuid

import pytest

from grid_api.routers import openai as o
from grid_api.services import credits, token_stream

MODEL = "gpt-oss-120b"


def _fake_subscribe(events):
    """Return an async-generator function that yields the given events."""
    async def gen(job_id, *a, **kw):
        for e in events:
            yield e
    return gen


@pytest.fixture
def capture_reconcile(monkeypatch):
    """Charging ON; record every reconcile(...) call instead of touching a DB."""
    monkeypatch.setattr(credits, "CHARGING_ENABLED", True)
    calls = []

    async def rec(user, model, p, c, reserved, job_id):
        calls.append({"p": p, "c": c, "reserved": reserved, "job_id": job_id})

    monkeypatch.setattr(credits, "reconcile", rec)
    return calls


@pytest.mark.asyncio
async def test_collect_bills_grid_count_not_worker_usage(monkeypatch, capture_reconcile):
    content = "hello world, this is the grid actually speaking back to you"
    events = [
        {"delta": {"content": content}},
        # Worker LIES: reports zero usage while real content was delivered.
        {"text": token_stream.DONE_SENTINEL, "full_text": content,
         "usage": {"prompt_tokens": 0, "completion_tokens": 0}, "finish_reason": "stop"},
    ]
    monkeypatch.setattr(o.token_stream, "subscribe_tokens", _fake_subscribe(events))

    resp = await o._collect_response(
        "job-c", MODEL, {"account_id": uuid.uuid4()}, seed=None, reserved=999, prompt_toks=42
    )

    assert len(capture_reconcile) == 1
    call = capture_reconcile[0]
    assert call["p"] == 42                              # grid prompt count, threaded in
    assert call["c"] == o.den.count_tokens(content)     # grid count, NOT worker's 0
    assert call["c"] > 0
    assert call["reserved"] == 999
    # Client-facing usage falls back to grid counts when the worker zeroes them.
    assert resp["usage"]["completion_tokens"] == o.den.count_tokens(content)


@pytest.mark.asyncio
async def test_stream_bills_grid_count_not_worker_usage(monkeypatch, capture_reconcile):
    parts = ["The last revolution ", "was metered in kWh, ", "this one in tokens."]
    events = [{"delta": {"content": p}} for p in parts] + [
        {"text": token_stream.DONE_SENTINEL,
         "usage": {"prompt_tokens": 5, "completion_tokens": 0}, "finish_reason": "stop"},
    ]
    monkeypatch.setattr(o.token_stream, "subscribe_tokens", _fake_subscribe(events))

    chunks = []
    async for c in o._stream_openai("job-s", MODEL, "cid", {"account_id": uuid.uuid4()},
                                    seed=None, reserved=500, prompt_toks=7):
        chunks.append(c)

    assert len(capture_reconcile) == 1
    call = capture_reconcile[0]
    assert call["p"] == 7
    assert call["c"] == o.den.count_tokens("".join(parts))  # grid count of relayed text
    assert call["c"] > 0


@pytest.mark.asyncio
async def test_stream_settles_on_disconnect(monkeypatch, capture_reconcile):
    """Client disconnects before DONE → finally settles the reservation."""
    events = [{"delta": {"content": "partial answer that the client "}},
              {"delta": {"content": "never finished reading"}},
              {"delta": {"content": "...and more"}}]
    monkeypatch.setattr(o.token_stream, "subscribe_tokens", _fake_subscribe(events))

    agen = o._stream_openai("job-d", MODEL, "cid", {"account_id": uuid.uuid4()},
                            seed=None, reserved=500, prompt_toks=3)
    await agen.__anext__()  # leading role chunk
    await agen.__anext__()  # first content delta relayed → "partial answer that the client "
    await agen.aclose()     # client goes away

    assert len(capture_reconcile) == 1          # settled in finally, not stranded
    call = capture_reconcile[0]
    assert call["p"] == 3
    # Billed only for what was actually relayed before the disconnect.
    assert call["c"] == o.den.count_tokens("partial answer that the client ")


@pytest.mark.asyncio
async def test_stream_worker_error_still_settles(monkeypatch, capture_reconcile):
    events = [{"text": token_stream.DONE_SENTINEL, "error": "backend exploded", "code": 502}]
    monkeypatch.setattr(o.token_stream, "subscribe_tokens", _fake_subscribe(events))

    chunks = []
    async for c in o._stream_openai("job-e", MODEL, "cid", {"account_id": uuid.uuid4()},
                                    seed=None, reserved=500, prompt_toks=9):
        chunks.append(c)

    # Settled exactly once; nothing relayed → completion 0 → reconcile refunds ~all.
    assert len(capture_reconcile) == 1
    assert capture_reconcile[0]["c"] == 0
    assert any("error" in ch for ch in chunks)


@pytest.mark.asyncio
async def test_settle_is_noop_in_dry_run(monkeypatch):
    """Dry-run: reconcile is never called; charge_request only logs."""
    monkeypatch.setattr(credits, "CHARGING_ENABLED", False)
    reconcile_calls, charge_calls = [], []

    async def rec(*a, **k):
        reconcile_calls.append(a)

    async def charge(*a, **k):
        charge_calls.append(a)

    monkeypatch.setattr(credits, "reconcile", rec)
    monkeypatch.setattr(credits, "charge_request", charge)
    events = [{"delta": {"content": "hi"}},
              {"text": token_stream.DONE_SENTINEL, "full_text": "hi", "finish_reason": "stop"}]
    monkeypatch.setattr(o.token_stream, "subscribe_tokens", _fake_subscribe(events))

    await o._collect_response("job-dry", MODEL, {"account_id": uuid.uuid4()},
                              seed=None, reserved=0, prompt_toks=1)
    assert reconcile_calls == []
    assert len(charge_calls) == 1
