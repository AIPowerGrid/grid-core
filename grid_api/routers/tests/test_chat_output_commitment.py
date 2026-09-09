# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression for the paid Chat image canary's tool-only missing commitment."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from grid_api.routers import worker_ws
from grid_api.services import chat_output, ledger, signing
from grid_api.services.den import count_tokens


def tool_call(arguments='{"prompt":"blue triangle"}'):
    return {"index": 0, "id": "call-test", "type": "function", "function": {"name": "image", "arguments": arguments}}


def test_plain_text_receipt_unchanged_and_empty_stays_absent():
    assert chat_output.result_hash("hello") == ledger.content_hash("hello")
    assert chat_output.result_hash("") is None
    assert chat_output.completion_tokens("") == 0


@pytest.mark.parametrize("content,reasoning,calls", [
    ("", "", [tool_call()]),
    ("", "Plan a picture.", [tool_call()]),
    ("", "Reasoning only", None),
    ("Here is the image.", "Plan a picture.", [tool_call()]),
])
def test_commitment_binds_all_channels(content, reasoning, calls):
    digest = chat_output.result_hash(content, reasoning, calls)
    assert digest == ledger.canonical_hash({
        "schema": "aipg.chat-output.v2", "content": content,
        "reasoning_content": reasoning, "tool_calls": calls or [],
    })
    assert digest != chat_output.result_hash(content + "different", reasoning, calls)
    assert digest != chat_output.result_hash(content, reasoning + "different", calls)
    assert digest != chat_output.result_hash(content, reasoning, [tool_call('{"prompt":"red square"}')])


def test_tool_meter_counts_name_and_arguments_not_transport_metadata():
    calls = [tool_call(), tool_call('{"prompt":"red square"}')]
    expected = count_tokens("answer") + count_tokens("reasoning") + sum(
        count_tokens(c["function"]["name"]) + count_tokens(c["function"]["arguments"]) for c in calls
    )
    assert chat_output.completion_tokens("answer", "reasoning", calls) == expected
    assert chat_output.completion_tokens("", "", calls) > 0
    calls[0]["id"] = "x" * 10000
    calls[0]["index"] = 1000000
    assert chat_output.completion_tokens("answer", "reasoning", calls) == expected


def test_old_visible_only_signature_does_not_authenticate_tool_output():
    wallet = Account.create()
    old_hash = ledger.content_hash("visible")
    signature = Account.sign_message(encode_defunct(text=f"aipg-job:job-test:{old_hash}"), wallet.key).signature.hex()
    assert signing.verify_worker_sig("job-test", old_hash, signature, [wallet.address])
    rich_hash = chat_output.result_hash("visible", "", [tool_call()])
    assert signing.verify_worker_sig("job-test", rich_hash, signature, [wallet.address]) is None
    rich_sig = Account.sign_message(encode_defunct(text=f"aipg-job:job-test:{rich_hash}"), wallet.key).signature.hex()
    assert signing.verify_worker_sig("job-test", rich_hash, rich_sig, [wallet.address])


async def generation(monkeypatch, events):
    ws = AsyncMock()
    ws.receive_json.side_effect = deepcopy(events)
    monkeypatch.setattr(worker_ws.token_stream, "publish_token", AsyncMock())
    monkeypatch.setattr(worker_ws.token_stream, "is_cancelled", AsyncMock(return_value=False))
    return await worker_ws._handle_worker_generation(ws, {"job_id": "job-test"}, {})


@pytest.mark.asyncio
async def test_tool_only_stream_is_committed_independently_of_chunks(monkeypatch):
    whole = [
        {"type": "token", "delta": {"tool_calls": [tool_call()]}},
        {"type": "done", "full_text": "", "usage": {"completion_tokens": 0}},
    ]
    split = [
        {"type": "token", "delta": {"tool_calls": [tool_call('{"prompt":')]}},
        {"type": "token", "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"blue triangle"}'}}]}},
        whole[-1],
    ]
    results = [await generation(monkeypatch, stream) for stream in (whole, split)]
    for result in results:
        assert not result["failed"]
        assert result["tool_calls"] == [tool_call()]
        assert chat_output.result_hash(result["full_text"], result["full_reasoning"], result["tool_calls"])
        assert chat_output.completion_tokens(result["full_text"], result["full_reasoning"], result["tool_calls"]) > 0
    assert results[0]["tool_calls"] == results[1]["tool_calls"]


@pytest.mark.asyncio
@pytest.mark.parametrize("delta", [{"content": "witnessed"}, {"reasoning_content": "witnessed"}, {"tool_calls": [tool_call()]}])
async def test_done_cannot_replace_witnessed_stream(monkeypatch, delta):
    result = await generation(monkeypatch, [
        {"type": "token", "delta": delta},
        {"type": "done", "full_text": "fabricated" * 100, "full_reasoning": "invented"},
    ])
    assert result["full_text"] == delta.get("content", "")
    assert result["full_reasoning"] == delta.get("reasoning_content", "")


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", [[], [{"type": "token", "delta": {"role": "assistant"}}]])
async def test_legacy_done_only_output_is_still_witnessed(monkeypatch, prefix):
    result = await generation(monkeypatch, prefix + [{"type": "done", "full_text": "legacy result"}])
    assert result["full_text"] == "legacy result"
    assert not result["failed"]
