"""Grok (xAI, OpenAI-compatible) adapter: internal tool_use/tool_result history <-> chat messages, no network."""
import json
from types import SimpleNamespace as NS

from src.modules.llm import grok_client


def test_history_maps_to_openai_messages():
    messages = [
        {"role": "user", "content": "markup?"},
        {"role": "assistant", "content": [NS(type="tool_use", id="call_1", name="search_policy", input={"query": "markup"})]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "<policy_excerpt/>"},
                                     {"type": "tool_result", "tool_use_id": "call_2", "content": "bad", "is_error": True}]},
    ]
    out = grok_client._to_messages("SYSTEM", messages)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "tool"]
    call = out[2]["tool_calls"][0]
    assert call["id"] == "call_1" and json.loads(call["function"]["arguments"]) == {"query": "markup"}
    assert out[3] == {"role": "tool", "tool_call_id": "call_1", "content": "<policy_excerpt/>"}
    assert out[4]["content"].startswith("ERROR: ")


def test_response_maps_tool_calls_and_usage():
    args = json.dumps({"status": "answered"})
    data = {"model": "grok-4-fast-non-reasoning",
            "choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
                {"id": "call_9", "type": "function", "function": {"name": "respond", "arguments": args}}]}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 80, "prompt_tokens_details": {"cached_tokens": 300}}}
    r = grok_client._to_response(data, "grok-4-fast-non-reasoning")
    assert r.stop_reason == "tool_use" and r.content[0].name == "respond" and r.content[0].input == {"status": "answered"}
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.cache_read_input_tokens) == (700, 80, 300)


def test_unparseable_arguments_become_a_schema_error_not_a_crash():
    data = {"choices": [{"message": {"tool_calls": [{"id": "c", "function": {"name": "respond", "arguments": "{oops"}}]}}]}
    r = grok_client._to_response(data, "grok")
    assert "_unparseable_arguments" in r.content[0].input


def test_content_filter_is_a_refusal():
    r = grok_client._to_response({"choices": [{"finish_reason": "content_filter", "message": {}}]}, "grok")
    assert r.stop_reason == "refusal"
