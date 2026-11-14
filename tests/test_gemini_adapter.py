"""Gemini adapter: internal tool_use/tool_result history <-> Gemini contents, without network."""
from types import SimpleNamespace as NS

from src.modules.llm import gemini_client


def test_history_maps_tool_use_and_result_with_names_and_signature():
    part = {"functionCall": {"name": "search_policy", "args": {"query": "markup"}}, "thoughtSignature": "sig"}
    messages = [
        {"role": "user", "content": "markup?"},
        {"role": "assistant", "content": [NS(type="thinking", thinking=""),
                                          NS(type="tool_use", id="gem_1", name="search_policy",
                                             input={"query": "markup"}, gemini_part=part)]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "gem_1", "content": "<policy_excerpt/>"}]},
    ]
    c = gemini_client._to_contents(messages)
    assert [x["role"] for x in c] == ["user", "model", "user"]
    assert c[1]["parts"] == [part]  # thinking dropped, original part (with thoughtSignature) resent
    assert c[2]["parts"][0]["functionResponse"]["name"] == "search_policy"


def test_response_maps_to_internal_shape_and_bills_thinking_as_output():
    data = {"candidates": [{"content": {"parts": [{"functionCall": {"name": "respond", "args": {"status": "answered"}}}]},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 50, "thoughtsTokenCount": 70,
                              "cachedContentTokenCount": 400}, "modelVersion": "gemini-2.5-flash"}
    r = gemini_client._to_response(data, "gemini-2.5-flash")
    assert r.stop_reason == "tool_use" and r.content[0].name == "respond" and r.content[0].input == {"status": "answered"}
    assert (r.usage.input_tokens, r.usage.output_tokens, r.usage.cache_read_input_tokens) == (600, 120, 400)


def test_safety_block_is_a_refusal():
    r = gemini_client._to_response({"candidates": [{"finishReason": "SAFETY"}]}, "gemini-2.5-flash")
    assert r.stop_reason == "refusal" and r.content == []


def test_tool_call_from_grok_gets_placeholder_signature():
    msgs = [{"role": "assistant", "content": [NS(type="tool_use", id="call_1", name="search_policy", input={"query": "x"})]}]
    part = gemini_client._to_contents(msgs)[0]["parts"][0]
    assert part["functionCall"]["name"] == "search_policy" and part["thoughtSignature"] == "skip_thought_signature_validator"


def test_http_errors_map_to_router_actions():
    from src.modules.llm.errors import ProviderError, ProviderRateLimited, ProviderRetryable, from_status

    assert isinstance(from_status("gemini", 429, "quota"), ProviderRateLimited)
    assert isinstance(from_status("gemini", 503, "overloaded"), ProviderRetryable)
    bad_key = from_status("gemini", 400, "API key not valid")
    assert isinstance(bad_key, ProviderError) and bad_key.permanent
    assert not from_status("grok", 400, "bad schema").permanent


def test_retry_after_is_read_from_header_or_provider_text():
    from src.modules.llm.errors import from_status

    groq = from_status("groq", 429, '{"error":{"message":"Rate limit reached ... Please try again in 7.66s."}}')
    assert abs(groq.retry_after - 7.66) < 1e-9
    assert from_status("gemini", 429, '{"retryDelay": "41s"}').retry_after == 41
    assert from_status("groq", 429, "slow down", {"retry-after": "3"}).retry_after == 3
    assert from_status("gemini", 429, "You exceeded your current quota").retry_after is None


def test_malformed_tool_call_from_model_is_retried_not_skipped():
    from src.modules.llm.errors import ProviderRetryable, from_status

    body = '{"error":{"message":"Tool choice is required, but model did not call a tool","code":"tool_use_failed"}}'
    assert type(from_status("groq", 400, body)) is ProviderRetryable
