# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

import copy
import json

import pytest

from grid_api.services.validator_responses import (
    MAX_ALTERNATIVES,
    MAX_EVENT_BYTES,
    MAX_POSITIONS,
    ObservationLimit,
    ResponsesObservation,
    read_logprobs,
)


def event():
    return {
        "type": "response.output_text.delta",
        "delta": "Blue",
        "logprobs": [
            {
                "token": "Blue",
                "logprob": -0.127,
                "bytes": [66, 108, 117, 101],
                "top_logprobs": [
                    {"token": "Blue", "logprob": -0.127, "bytes": [66, 108, 117, 101]},
                    {"token": "Green", "logprob": -4.125, "bytes": None},
                ],
            },
        ],
    }


def test_preserves_selected_and_alternatives_without_renormalization():
    original = event()
    before = copy.deepcopy(original)
    result = read_logprobs(json.dumps(original))
    assert result["status"] == "available"
    assert result["positions"] == original["logprobs"]
    assert original == before


@pytest.mark.parametrize("value", [None, []])
def test_missing_is_unavailable_not_failed(value):
    source = event()
    source["logprobs"] = value
    assert read_logprobs(json.dumps(source)) == {
        "positions": [],
        "status": "unavailable",
        "reason": "missing_logprobs",
    }


def test_absent_field_is_unavailable():
    assert read_logprobs('{"type":"response.output_text.delta"}')["status"] == "unavailable"


@pytest.mark.parametrize("kind", ["response.completed", "response.output_text.done", "response.reasoning_text.delta"])
def test_ignores_terminal_duplicates_and_reasoning(kind):
    source = event()
    source["type"] = kind
    assert read_logprobs(json.dumps(source))["reason"] == "not_output_delta"


@pytest.mark.parametrize("number", [True, "-0.1", None, float("nan"), float("inf"), float("-inf"), 0.01, -1_000_001, 10**1000])
def test_rejects_invalid_probability(number):
    source = event()
    source["logprobs"][0]["logprob"] = number
    result = read_logprobs(json.dumps(source))
    assert result["status"] == "unavailable"
    assert result["positions"] == []


@pytest.mark.parametrize("value", ["x", {}, [None], [1], [[1]]])
def test_malformed_positions(value):
    source = event()
    source["logprobs"] = value
    assert read_logprobs(json.dumps(source))["status"] == "unavailable"


@pytest.mark.parametrize("token", [None, 4, "x" * 129, "\u00e9" * 65, "\ud800"])
def test_token_size_and_unicode_bounds(token):
    source = event()
    source["logprobs"][0]["token"] = token
    assert read_logprobs(json.dumps(source))["status"] == "unavailable"


@pytest.mark.parametrize("raw", [[True], [-1], [256], [0] * 129, "bytes"])
def test_token_byte_bounds(raw):
    source = event()
    source["logprobs"][0]["bytes"] = raw
    assert read_logprobs(json.dumps(source))["status"] == "unavailable"


def test_rejects_excess_alternatives_and_positions_not_silent_truncation():
    source = event()
    source["logprobs"][0]["top_logprobs"] = [{"token": "x", "logprob": -1}] * (MAX_ALTERNATIVES + 1)
    assert read_logprobs(json.dumps(source))["status"] == "unavailable"
    source = event()
    source["logprobs"] *= MAX_POSITIONS + 1
    assert read_logprobs(json.dumps(source))["status"] == "unavailable"


@pytest.mark.parametrize("raw", [None, [], "not json", "[]", "null", "{" * 1000, "[" * 2000 + "]" * 2000, " " * (MAX_EVENT_BYTES + 1)])
def test_malformed_or_oversized_event_is_bounded_unavailable(raw):
    assert read_logprobs(raw)["status"] == "unavailable"


def test_unknown_nested_fields_not_retained():
    source = event()
    source["private_extra"] = {"arbitrary": [1, 2, 3]}
    source["logprobs"][0]["extra"] = {"arbitrary": [1, 2, 3]}
    result = read_logprobs(json.dumps(source))
    assert result["positions"] == event()["logprobs"]


def stream_delta(**changes):
    return {**event(), "sequence_number": 1, "item_id": "msg-test", "output_index": 0, "content_index": 0, **changes}


@pytest.mark.parametrize(
    "changes",
    [
        {"sequence_number": True},
        {"sequence_number": -1},
        {"sequence_number": 2**53},
        {"item_id": "x" * 129},
        {"item_id": "\u00e9" * 65},
        {"item_id": "\ud800"},
        {"output_index": True},
        {"content_index": -1},
        {"delta": []},
    ],
)
def test_bad_delta_binding_is_unavailable(changes):
    observation = ResponsesObservation()
    observation.add(json.dumps(stream_delta(**changes)))
    observation.add('{"type":"response.completed"}')
    assert observation.result()["status"] == "unavailable"
    assert observation.result()["reason"] == "malformed_event"


@pytest.mark.parametrize("item_id", ["x" * 128, "\u00e9" * 64])
def test_item_id_accepts_exact_utf8_byte_limit(item_id):
    observation = ResponsesObservation()
    observation.add(json.dumps(stream_delta(item_id=item_id)))
    observation.add('{"type":"response.completed"}')
    assert observation.result()["status"] == "available"
    assert observation.events[0]["item_id"] == item_id


@pytest.mark.parametrize(
    "old,new",
    [
        ('"sequence_number": 1', '"sequence_number": 2, "sequence_number": 1'),
        ('"logprob": -0.127', '"logprob": -8, "logprob": -0.127'),
        ('"logprob": -4.125', '"logprob": -9, "logprob": -4.125'),
    ],
)
def test_duplicate_json_keys_are_not_last_value_wins(old, new):
    raw = json.dumps(stream_delta())
    assert old in raw
    raw = raw.replace(old, new)
    assert read_logprobs(raw)["status"] == "unavailable"
    observation = ResponsesObservation()
    observation.add(raw)
    observation.add('{"type":"response.completed"}')
    assert observation.result()["status"] == "unavailable"
    assert observation.result()["reason"] == "malformed_event"
    assert observation.events == []


def test_replayed_sequence_is_not_counted_twice():
    observation = ResponsesObservation()
    source = json.dumps(stream_delta())
    observation.add(source)
    observation.add(source)
    observation.add('{"type":"response.completed"}')
    assert len(observation.events) == 1
    assert observation.result()["status"] == "unavailable"


def test_terminal_logprob_copies_are_ignored():
    observation = ResponsesObservation()
    observation.add(json.dumps(stream_delta()))
    observation.add(json.dumps({**event(), "type": "response.output_text.done"}))
    observation.add(json.dumps({**event(), "type": "response.completed"}))
    assert observation.result()["status"] == "available"
    assert len(observation.events) == 1


def test_multiple_message_parts_not_concatenated_as_one_context():
    observation = ResponsesObservation()
    observation.add(json.dumps(stream_delta()))
    observation.add(json.dumps(stream_delta(sequence_number=2, item_id="other")))
    observation.add('{"type":"response.completed"}')
    assert observation.text == "Blue"
    assert observation.result()["reason"] == "multiple_output_parts"


def test_total_probability_limit_is_not_per_event_only():
    observation = ResponsesObservation()
    for n in range(MAX_POSITIONS):
        observation.add(json.dumps(stream_delta(sequence_number=n)))
    with pytest.raises(ObservationLimit):
        observation.add(json.dumps(stream_delta(sequence_number=MAX_POSITIONS)))


@pytest.mark.parametrize("which", ["MAX_STREAM_BYTES", "MAX_STREAM_EVENTS", "MAX_OUTPUT_BYTES", "MAX_DELTA_EVENTS"])
def test_whole_stream_limits(monkeypatch, which):
    from grid_api.services import validator_responses

    monkeypatch.setattr(validator_responses, which, 0)
    with pytest.raises(ObservationLimit):
        ResponsesObservation().add(json.dumps(stream_delta()))
