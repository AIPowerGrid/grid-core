# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`POST /v1/embeddings`: request contract, input-only metering, worker pool
selection and the shape check that runs before a worker is paid."""

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from grid_api.config import GridSettings
from grid_api.routers import _passthrough as pt
from grid_api.routers import embeddings, worker_ws
from grid_api.services import credits, den, generation_admission, token_stream

FMT = "openai-embeddings"
MODEL = "nomic-embed-text"
route = embeddings.create_embeddings.__wrapped__  # the handler without the rate limiter


def configure(monkeypatch, paths):
    settings = GridSettings(_env_file=None, generation_enabled_paths=paths)
    monkeypatch.setattr(generation_admission, "get_settings", lambda: settings)


def wire(monkeypatch, *, online=(MODEL,), auth=None, full=None):
    """Stub everything around the handler; return the mocks that must (not) be hit."""
    user = {"account_id": uuid.uuid4()}
    monkeypatch.setattr(embeddings.accounts_svc, "authenticate", AsyncMock(return_value=user))
    lookup = AsyncMock(return_value=list(online))
    monkeypatch.setattr(embeddings, "get_available_models", lookup)
    consume = AsyncMock()
    monkeypatch.setattr(embeddings.quota, "check_and_consume", consume)
    reserve = AsyncMock(return_value=auth or {"ok": True, "reserved": 0, "status": "dry_run"})
    monkeypatch.setattr(credits, "authorize_request", reserve)
    monkeypatch.setattr(pt.prompt_screen, "observe", lambda *a, **k: None)
    submit = AsyncMock()
    monkeypatch.setattr(pt.job_queue, "submit_job", submit)

    async def subscribe(job_id, *a, **k):
        yield {"text": token_stream.DONE_SENTINEL, "full_json": full or {}}

    monkeypatch.setattr(pt.token_stream, "subscribe_tokens", subscribe)
    monkeypatch.setattr(credits, "CHARGING_ENABLED", False)
    monkeypatch.setattr(credits, "charge_request", AsyncMock(return_value={"status": "dry_run"}))
    return lookup, consume, reserve, submit


async def call(body):
    return await route(None, body=body, apikey="k", authorization=None,
                       x_grid_user_assertion=None, x_grid_user_token=None)


# ── request contract ─────────────────────────────────────────────────────────


def test_normalize_forwards_only_known_fields():
    out = embeddings.normalize_embeddings_request({
        "model": MODEL, "input": ["a", "b"], "encoding_format": "base64", "dimensions": 256,
        "stream": True, "user": "someone", "max_tokens": 9,
    })
    assert out == {"model": MODEL, "input": ["a", "b"], "encoding_format": "base64", "dimensions": 256}


def test_normalize_keeps_a_single_string_as_a_string():
    assert embeddings.normalize_embeddings_request({"model": MODEL, "input": "hello"})["input"] == "hello"


@pytest.mark.parametrize("body", [
    {"input": "x"},
    {"model": "", "input": "x"},
    {"model": MODEL},
    {"model": MODEL, "input": ""},
    {"model": MODEL, "input": []},
    {"model": MODEL, "input": ["ok", ""]},
    {"model": MODEL, "input": [1, 2, 3]},
    {"model": MODEL, "input": [[1, 2], [3]]},
    {"model": MODEL, "input": {"text": "x"}},
    {"model": MODEL, "input": "x", "encoding_format": "hex"},
    {"model": MODEL, "input": "x", "dimensions": 0},
    {"model": MODEL, "input": "x", "dimensions": True},
    {"model": MODEL, "input": "x", "dimensions": "256"},
    {"model": MODEL, "input": ["x"] * (embeddings.MAX_INPUTS + 1)},
])
def test_normalize_rejects_malformed_requests(body):
    with pytest.raises(HTTPException) as error:
        embeddings.normalize_embeddings_request(body)
    assert error.value.status_code == 400


def test_batch_at_the_limit_is_accepted():
    body = {"model": MODEL, "input": ["x"] * embeddings.MAX_INPUTS}
    assert embeddings.input_count(embeddings.normalize_embeddings_request(body)) == embeddings.MAX_INPUTS


# ── metering: input only ─────────────────────────────────────────────────────


def test_prompt_text_is_the_input_and_nothing_else():
    assert pt.extract_prompt_text(FMT, {"model": MODEL, "input": "one two"}) == "one two"
    assert pt.extract_prompt_text(FMT, {"model": MODEL, "input": ["one", "two three"]}) == "one two three"
    assert pt.extract_prompt_text(FMT, {"model": MODEL}) == ""


def test_completion_is_never_counted_for_embeddings():
    full = {"data": [{"embedding": [0.1, 0.2]}], "output_text": "a long made-up answer " * 50}
    assert pt.completion_tokens(FMT, None, full) == 0
    assert pt.completion_tokens(FMT, [json.dumps({"delta": "text " * 50})], None) == 0
    assert pt.extract_output_text(FMT, full) == ""


# ── shape check before settlement ────────────────────────────────────────────


V2 = [0.1, -2, 3.5]
B64 = "AACAPwAAAEA="        # two float32 values


@pytest.mark.parametrize("request_,full,ok", [
    ({"input": "a"}, {"data": [{"embedding": V2}]}, True),
    ({"input": ["a", "b"]}, {"data": [{"embedding": V2, "index": 0}, {"embedding": V2, "index": 1}]}, True),
    ({"input": "a", "dimensions": 3}, {"data": [{"embedding": V2}]}, True),
    ({"input": ["a", "b"], "encoding_format": "base64"}, {"data": [{"embedding": B64}, {"embedding": B64}]}, True),
    ({"input": ["a", "b"]}, {"data": [{"embedding": V2}]}, False),                          # too few
    ({"input": "a"}, {"data": [{"embedding": V2}, {"embedding": V2}]}, False),              # too many
    ({"input": ["a", "b"]}, {"data": [{"embedding": V2}, {"embedding": [0.1]}]}, False),    # mixed lengths
    ({"input": ["a", "b"]}, {"data": [{"embedding": V2, "index": 1}, {"embedding": V2, "index": 0}]}, False),
    ({"input": "a", "dimensions": 2}, {"data": [{"embedding": V2}]}, False),                # not the asked size
    ({"input": "a"}, {"data": [{"embedding": [0.0] * 8193}]}, False),                       # oversized vector
    ({"input": "a"}, {"data": [{"embedding": B64}]}, False),                                # base64 not asked
    ({"input": "a", "encoding_format": "base64"}, {"data": [{"embedding": V2}]}, False),
    ({"input": "a", "encoding_format": "base64"}, {"data": [{"embedding": "not base64 !!"}]}, False),
    ({"input": "a", "encoding_format": "base64"}, {"data": [{"embedding": "AAA="}]}, False),  # not whole floats
    ({"input": "a", "encoding_format": "base64"}, {"data": [{"embedding": "A" * 50000}]}, False),
    ({"input": "a"}, {"data": [{"embedding": []}]}, False),
    ({"input": "a"}, {"data": [{"embedding": [0.1, "x"]}]}, False),
    ({"input": "a"}, {"data": [{"embedding": [0.1, True]}]}, False),
    ({"input": "a"}, {"data": [{"embedding": [float("nan")]}]}, False),
    ({"input": "a"}, {"data": [{"embedding": [float("inf")]}]}, False),
    ({"input": "a"}, {"data": [{"vector": [0.1]}]}, False),
    ({"input": "a"}, {"data": ["x"]}, False),
    ({"input": "a"}, {"data": {"embedding": [0.1]}}, False),
    ({"input": "a"}, {}, False),
    ({"input": "a"}, None, False),
    ({"input": "a"}, "text", False),
])
def test_embeddings_response_shape(request_, full, ok):
    assert pt.embeddings_response_ok(full, request_) is ok


# ── worker pool ──────────────────────────────────────────────────────────────


def fake_redis(monkeypatch, snapshots):
    class Pipeline:
        def get(self, _key):
            return self

        async def execute(self):
            return [json.dumps(s) for s in snapshots]

    class Redis:
        async def smembers(self, _key):
            return [f"w{i}" for i in range(len(snapshots))]

        def pipeline(self):
            return Pipeline()

    monkeypatch.setattr(worker_ws, "get_redis", lambda: Redis())


@pytest.mark.asyncio
async def test_embedding_only_worker_stays_out_of_the_chat_model_list(monkeypatch):
    fake_redis(monkeypatch, [
        {"job_types": ["text"], "models": ["chat-model"]},  # legacy worker, no formats
        {"job_types": ["text"], "models": ["both-model"], "api_formats": ["openai-chat", FMT]},
        {"job_types": ["text"], "models": [MODEL], "api_formats": [FMT]},
    ])
    assert await worker_ws.get_available_models(job_type="text") == ["both-model", "chat-model"]
    assert await worker_ws.get_available_models(job_type="text", api_format="openai-chat") == [
        "both-model", "chat-model"]
    assert await worker_ws.get_available_models(job_type="text", api_format=FMT) == ["both-model", MODEL]
    # The unfiltered inventory (health) still counts every served model.
    assert MODEL in await worker_ws.get_available_models()


# ── the route ────────────────────────────────────────────────────────────────


def test_path_is_closed_by_default():
    assert FMT not in GridSettings(_env_file=None).generation_enabled_paths


@pytest.mark.asyncio
async def test_closed_path_refuses_before_lookup_quota_or_reserve(monkeypatch):
    configure(monkeypatch, ["openai-chat"])
    lookup, consume, reserve, submit = wire(monkeypatch)
    with pytest.raises(HTTPException) as error:
        await call({"model": MODEL, "input": "hello"})
    assert error.value.status_code == 503
    for mock in (lookup, consume, reserve, submit):
        mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_embedding_worker_is_503_and_unknown_model_is_404(monkeypatch):
    configure(monkeypatch, [FMT])
    _, consume, reserve, submit = wire(monkeypatch, online=())
    with pytest.raises(HTTPException) as error:
        await call({"model": MODEL, "input": "hello"})
    assert error.value.status_code == 503

    wire(monkeypatch, online=("other-embed",))
    with pytest.raises(HTTPException) as error:
        await call({"model": MODEL, "input": "hello"})
    assert error.value.status_code == 404 and "other-embed" in error.value.detail
    for mock in (consume, reserve, submit):
        mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_request_consumes_no_quota(monkeypatch):
    configure(monkeypatch, [FMT])
    lookup, consume, reserve, submit = wire(monkeypatch)
    with pytest.raises(HTTPException) as error:
        await call({"model": MODEL, "input": [1, 2, 3]})
    assert error.value.status_code == 400
    for mock in (lookup, consume, reserve, submit):
        mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_insufficient_funds_is_402_and_nothing_is_dispatched(monkeypatch):
    configure(monkeypatch, [FMT])
    _, _, _, submit = wire(monkeypatch, auth={"ok": False, "reserved": 0, "reason": "insufficient balance"})
    with pytest.raises(HTTPException) as error:
        await call({"model": MODEL, "input": "hello"})
    assert error.value.status_code == 402
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_reserves_on_grid_count_with_no_output_budget(monkeypatch):
    configure(monkeypatch, [FMT])
    texts = ["The grid counts this text.", "And this one too."]
    worker_body = {
        "object": "list", "model": "backend-internal-name",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]},
                 {"object": "embedding", "index": 1, "embedding": [0.3, 0.4]}],
        "usage": {"prompt_tokens": 999999, "total_tokens": 999999},  # worker LIES
    }
    _, consume, reserve, submit = wire(monkeypatch, full=worker_body)

    response = await call({"model": MODEL, "input": texts, "stream": True, "user": "u"})
    body = json.loads(response.body)

    counted = den.count_tokens(" ".join(texts))
    assert counted > 0
    # reserve(user, model, prompt_tokens, max_tokens, job_id, ...)
    args = reserve.await_args.args
    assert args[1] == MODEL and args[2] == counted and args[3] == 0
    consume.assert_awaited_once()

    job_id, payload, models = submit.await_args.args
    assert models == [MODEL] and payload["api_format"] == FMT and payload["max_length"] == 0
    assert payload["request"] == {"model": MODEL, "input": texts}  # no stream, no user

    assert body["model"] == MODEL and body["object"] == "list"
    assert body["data"] == worker_body["data"]
    assert body["usage"] == {"prompt_tokens": counted, "total_tokens": counted}


@pytest.mark.asyncio
async def test_worker_failure_surfaces_its_status(monkeypatch):
    configure(monkeypatch, [FMT])
    wire(monkeypatch)

    async def subscribe(job_id, *a, **k):
        yield {"text": token_stream.DONE_SENTINEL, "error": "backend rejected request", "code": 400}

    monkeypatch.setattr(pt.token_stream, "subscribe_tokens", subscribe)
    with pytest.raises(HTTPException) as error:
        await call({"model": MODEL, "input": "hello"})
    assert error.value.status_code == 400


# ── worker socket terminal: pay only for one vector per input ───────────────


class _WebSocket:
    def __init__(self, incoming: list[dict]):
        self.incoming = list(incoming)
        self.sent: list[dict] = []

    async def send_json(self, value: dict):
        self.sent.append(value)

    async def receive_json(self):
        return self.incoming.pop(0)


async def _run_terminal(monkeypatch, full_json, usage=None):
    job_id = str(uuid.uuid4())
    job = {"job_id": job_id, "payload": {
        "api_format": FMT, "max_length": 0, "request": {"model": MODEL, "input": ["first", "second"]},
    }}
    settled, released, errors, done = [], [], [], []

    async def record_and_settle(**kwargs):
        settled.append(kwargs)
        return "settled"

    async def release_job(released_id):
        released.append(released_id)

    async def publish_error(error_id, message, code=None):
        errors.append((error_id, message, code))

    async def publish_done(*args, **kwargs):
        done.append((args, kwargs))

    async def clear_strikes(_worker_id):
        return None

    monkeypatch.setattr(worker_ws.credits, "record_and_settle", record_and_settle)
    monkeypatch.setattr(worker_ws.credits, "release_job", release_job)
    monkeypatch.setattr(worker_ws.token_stream, "publish_error", publish_error)
    monkeypatch.setattr(worker_ws.token_stream, "publish_done", publish_done)
    monkeypatch.setattr(worker_ws.token_stream, "is_cancelled", AsyncMock(return_value=False))
    monkeypatch.setenv("GRID_SALT", "embeddings-test-only-salt")
    strikes = AsyncMock(return_value=1)
    monkeypatch.setattr(worker_ws, "_record_strike", strikes)
    monkeypatch.setattr(worker_ws, "_clear_strikes", clear_strikes)
    monkeypatch.setattr(worker_ws, "record_job_complete", lambda **kwargs: None)
    monkeypatch.setattr(worker_ws, "record_job_failed", lambda **kwargs: None)

    ws = _WebSocket([{"type": "done", "usage": usage, "full_json": full_json}])
    finished = await worker_ws._handle_raw_passthrough(
        ws, job, MODEL, "worker-embed",
        {"name": "embed-rig", "wallet_address": "0x" + "2" * 40, "max_context_length": 8192},
    )
    return job_id, finished, ws.sent, settled, released, errors, done, strikes


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [
    None,
    {"prompt_tokens": 3},
    {"prompt_tokens": 999999, "completion_tokens": 4096},     # worker claims output an embedding cannot have
    {"input_tokens": 8192, "output_tokens": 32768},
])
async def test_terminal_ignores_the_workers_usage_report(monkeypatch, usage):
    full = {"data": [{"embedding": [0.1, 0.2]}, {"embedding": [0.3, 0.4]}]}
    job_id, finished, sent, settled, released, errors, done, strikes = await _run_terminal(
        monkeypatch, full, usage=usage)
    assert finished is True
    assert len(settled) == 1 and settled[0]["completion_tokens"] == 0
    assert settled[0]["ledger_values"]["output_units"] == 0
    assert settled[0]["ledger_values"]["den"] == worker_ws.calculate_den(output_tokens=0)   # the minimum award
    assert sent[-1]["den"] == settled[0]["ledger_values"]["den"]
    strikes.assert_not_awaited()
    assert done and done[0][1]["full_json"] == full
    assert not released and not errors
    assert sent[-1]["type"] == "ack"


@pytest.mark.asyncio
@pytest.mark.parametrize("full", [
    {"data": []},
    {"data": [{"embedding": [0.1]}]},                                  # one vector, two inputs
    {"data": [{"embedding": ["a"]}, {"embedding": ["b"]}]},
    {"output_text": "not vectors at all"},
    None,
])
async def test_terminal_refunds_strikes_and_does_not_pay_a_malformed_body(monkeypatch, full):
    job_id, finished, sent, settled, released, errors, done, strikes = await _run_terminal(monkeypatch, full)
    strikes.assert_awaited_once()
    assert finished is True
    assert not settled and not done
    assert released == [job_id]
    assert errors == [(job_id, "Worker returned a malformed embeddings response.", 502)]
    assert sent[-1] == {"type": "ack", "id": job_id, "den": 0}


# ── /v1/status/models ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_lists_an_embeddings_only_model_as_its_own_type(monkeypatch):
    from grid_api.routers import stats

    workers = [
        {"models": ["nomic-embed-text"], "job_types": ["text"], "api_formats": ["openai-embeddings"],
         "max_context_length": 2048},
        {"models": ["chat-model"], "job_types": ["text"], "api_formats": ["openai-chat"]},
        {"models": ["legacy-model"], "job_types": ["text"]},                      # no api_formats: a chat worker
        {"models": ["both"], "job_types": ["text"], "api_formats": ["openai-embeddings"]},
        {"models": ["both"], "job_types": ["text"], "api_formats": ["openai-chat"]},
    ]

    class _Session:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return None

    async def active_workers():
        return workers

    async def new_session():
        return _Session()

    async def perf_by_model(_session, _since):
        return {}

    monkeypatch.setattr(stats, "_active_workers", active_workers)
    monkeypatch.setattr(stats, "new_session", new_session)
    monkeypatch.setattr(stats, "_perf_by_model", perf_by_model)
    monkeypatch.setattr("grid_api.services.recipes.list_recipes", lambda: [])
    monkeypatch.setattr("grid_api.services.recipes.generation_modes", lambda _model: [])

    listed = {item["name"]: item for item in await stats.status_models()}

    assert listed["nomic-embed-text"]["type"] == "embedding"
    assert listed["nomic-embed-text"]["max_context_length"] == 2048
    assert "pricing" in listed["nomic-embed-text"]
    assert {listed[name]["type"] for name in ("chat-model", "legacy-model", "both")} == {"text"}
