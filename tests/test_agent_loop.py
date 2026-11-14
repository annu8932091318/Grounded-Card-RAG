"""The agent loop with a scripted fake model (no API key, no DB): happy path, repair, safe fallback,
static mode, model fallback, customer binding."""
from types import SimpleNamespace as NS

import pytest

from src.const.config import config
from src.modules.agent import agent
from src.modules.llm import gemini_client, grok_client, llm_router
from src.modules.llm.errors import ProviderRetryable

FEE_CHUNK = {"chunk_id": 1, "doc_id": "POL-FEE-007", "section": "Table", "version": "5.1",
             "text": "POL-FEE-007 Fees and Charges Schedule §Table: | Foreign currency markup | 3.5% |",
             "scores": {"kw": 0.2, "vec": 0.24, "rrf": 0.03}}


def _resp(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop, model="gemini-2.5-flash",
              usage=NS(input_tokens=1000, output_tokens=100, cache_read_input_tokens=800, cache_creation_input_tokens=0))


def _tool(name, args, tid):
    return NS(type="tool_use", id=tid, name=name, input=args)


def _respond(text, tid="r1", **extra):
    payload = {"status": "answered", "language": "en",
               "claims": [{"id": "c1", "type": "policy_fact", "text": text,
                           "citations": [{"kind": "policy", "doc_id": "POL-FEE-007", "section": "Table", "quote": None}]}],
               "decision": {"outcome": "none", "reason_code": None}, "routing": None, "unanswered": [],
               "conflicts_noted": []}
    payload.update(extra)
    return _tool("respond", payload, tid)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No DB, no network: retrieval returns the fee chunk; persistence/SR repo calls fail fast or are faked."""
    monkeypatch.setattr(agent.retriever, "search", lambda q, t, k=None: {
        "query": q, "grounded": True, "top_vec_sim": 0.24, "chunks": [FEE_CHUNK], "near_misses": [],
        "conflicts": [], "cache_hit": False})
    monkeypatch.setattr(agent, "_persist_conversation", lambda *a: None)
    monkeypatch.setattr(agent, "_routing_policy_chunk", lambda turn: None)
    from src.db.repositories import service_request_repo as sr
    monkeypatch.setattr(sr, "find_recent", lambda k: None)
    monkeypatch.setattr(sr, "insert_pending", lambda *a: None)
    monkeypatch.setattr(sr, "mark", lambda *a: None)
    from src.db.repositories import trace_repo
    monkeypatch.setattr(trace_repo, "save", lambda *a: None)
    monkeypatch.setattr(config, "answer_cache_ttl_s", 0)
    monkeypatch.setattr(config, "llm_force_primary_fail", False)
    monkeypatch.setattr(config, "llm_disabled", False)
    # Chain = one Gemini model (primary) -> one Grok model (fallback); _script fakes both, no network.
    monkeypatch.setattr(config, "gemini_api_key", "test-key")
    monkeypatch.setattr(config, "gemini_models", ["gemini-2.5-flash"])
    monkeypatch.setattr(config, "grok_api_key", "test-key")
    monkeypatch.setattr(config, "grok_models", ["grok-4-fast-non-reasoning"])
    monkeypatch.setattr(config, "grok_base_url", "https://api.x.ai/v1")  # independent of the local .env
    llm_router._breakers.clear()


def _script(monkeypatch, responses):
    calls = []

    def fake_call(model, system, tools, messages):
        calls.append({"model": model, "messages": list(messages)})
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(gemini_client, "call", fake_call)
    monkeypatch.setattr(grok_client, "call", fake_call)
    return calls


def test_happy_path_renders_message_from_validated_claims(monkeypatch):
    _script(monkeypatch, [
        _resp(_tool("search_policy", {"query": "foreign currency markup"}, "t1")),
        _resp(_respond("The foreign currency markup is 3.5%.")),
    ])
    out = agent.run_agent("CUST-2007", "What is the markup if I use my card in Singapore?")
    assert out["status"] == "answered"
    assert out["customer_message"] == "The foreign currency markup is 3.5%."
    assert out["meta"]["llm_calls"] == 2 and out["meta"]["tokens"]["cache_read"] == 1600
    assert out["meta"]["cost_usd"] > 0


def test_invented_number_is_repaired(monkeypatch):
    calls = _script(monkeypatch, [
        _resp(_tool("search_policy", {"query": "markup"}, "t1")),
        _resp(_respond("The foreign currency markup is 4.5%.")),  # fabricated
        _resp(_respond("The foreign currency markup is 3.5%.", tid="r2")),
    ])
    out = agent.run_agent("CUST-2007", "markup in Singapore?")
    assert out["status"] == "answered" and "citation_repair" in out["meta"]["flags"]
    repair_msg = calls[2]["messages"][-1]["content"][-1]
    assert repair_msg["is_error"] and "4.5" in repair_msg["content"]


def test_two_invalid_answers_give_safe_refusal_routed_to_general(monkeypatch):
    _script(monkeypatch, [
        _resp(_tool("search_policy", {"query": "markup"}, "t1")),
        _resp(_respond("Markup is 9%.")),
        _resp(_respond("Markup is 9%.", tid="r2")),
    ])
    out = agent.run_agent("CUST-2007", "markup?")
    assert out["status"] == "refused" and out["routing"]["queue"] == "GENERAL"
    assert "won't guess" in out["customer_message"] and "citation_validation_failed" in out["meta"]["flags"]


def test_all_models_down_gives_static_mode(monkeypatch):
    err = ProviderRetryable("network down")
    _script(monkeypatch, [err] * 4)  # gemini: 2 attempts, grok: 2 attempts
    monkeypatch.setattr(llm_router, "sleep", lambda s: None)
    out = agent.run_agent("CUST-2007", "foreign currency markup?")
    assert out["status"] == "degraded" and "static_mode" in out["meta"]["flags"]
    assert any(c.get("doc_id") == "POL-FEE-007" for c in out["citations_flat"])


def test_account_timeout_with_bad_answers_gives_honest_degraded_message(monkeypatch):
    monkeypatch.setattr(config, "tool_fail_rate", 1.0)
    _script(monkeypatch, [
        _resp(_tool("get_account_state", {}, "t1")),
        _resp(_respond("Your due date is 8 September.")),            # guessed -> invalid
        _resp(_respond("Your due date is 8 September.", tid="r2")),  # still invalid
    ])
    out = agent.run_agent("CUST-2000", "When is my payment due?")
    assert out["status"] == "degraded" and "couldn't retrieve your account details" in out["customer_message"]
    assert "September" not in out["customer_message"] and "tool_timeout" in out["meta"]["flags"]


def test_request_bookkeeping_is_completed_from_the_real_tool_result(make_turn):
    turn = make_turn("CUST-2000")
    turn.service_requests["SR-0A1B2C3D"] = {"request_id": "SR-0A1B2C3D", "queue": "SELFSERVE", "sla_hours": 24,
                                            "sla": "1 working day"}
    raw = {"status": "request_raised", "routing": {"queue": "SELFSERVE", "request_id": "SR-INVENTED", "sla": None},
           "claims": [{"id": "c1", "type": "next_step", "text": "Your statement request is in.",
                       "citations": [{"kind": "tool", "tool": "raise_service_request", "request_id": "SR-INVENTED"}]}]}
    out = agent._complete_routing(raw, turn)
    assert out["routing"]["request_id"] == "SR-0A1B2C3D"
    assert out["claims"][0]["citations"][0]["request_id"] == "SR-0A1B2C3D"

    raw2 = {"claims": [{"id": "c1", "type": "next_step", "text": "Done.", "citations": []}]}
    out2 = agent._complete_routing(raw2, turn)
    assert out2["claims"][-1]["id"] == "routing" and "1 working day" in out2["claims"][-1]["text"]


def test_malformed_claims_do_not_crash(monkeypatch):
    _script(monkeypatch, [
        _resp(_tool("search_policy", {"query": "markup"}, "t1")),
        _resp(_tool("respond", {"status": "answered", "claims": ["just a string"], "decision": {}}, "r1")),
        _resp(_respond("The foreign currency markup is 3.5%.", tid="r2")),
    ])
    out = agent.run_agent("CUST-2007", "markup?")
    assert out["status"] == "answered" and "citation_repair" in out["meta"]["flags"]


def test_static_mode_survives_a_long_policy_section(monkeypatch):
    """Regression (eval EV-015/017): a whole section > 500 chars crashed static mode."""
    long_chunk = {**FEE_CHUNK, "text": "POL-FEE-007 Fees §Table: " + "| Charge | 3.5% |\n" * 60}
    monkeypatch.setattr(agent.retriever, "search", lambda q, t, k=None: {
        "query": q, "grounded": True, "top_vec_sim": 0.3, "chunks": [long_chunk], "near_misses": [],
        "conflicts": [], "cache_hit": False})
    monkeypatch.setattr(config, "llm_disabled", True)
    out = agent.run_agent("CUST-2007", "fees?")
    assert out["status"] == "degraded"


def test_coverage_gap_is_routed_to_general(make_turn):
    turn = make_turn("CUST-2009")
    turn.misses.append("travel insurance")
    raw = {"status": "answered_with_gap", "claims": [{"id": "c1", "type": "gap", "text": "Not covered.",
                                                      "citations": [{"kind": "retrieval_miss", "query": "travel insurance"}]}]}
    out = agent._complete_routing(raw, turn)
    assert out["routing"]["queue"] == "GENERAL" and out["claims"][-1]["id"] == "routing"


def test_unknown_customer_is_404_even_in_static_mode(monkeypatch):
    """Regression (found in the live API test): static mode used to answer CUST-9999 and open an SR for it."""
    from src.modules.tools.errors import CustomerNotFound

    monkeypatch.setattr(config, "llm_disabled", True)
    with pytest.raises(CustomerNotFound):
        agent.run_agent("CUST-9999", "What is the markup?")


def test_static_mode_never_truncates_policy_text(monkeypatch):
    monkeypatch.setattr(config, "llm_disabled", True)
    out = agent.run_agent("CUST-2007", "markup?")
    assert "3.5%" in out["customer_message"]


def test_primary_outage_uses_fallback_model(monkeypatch):
    monkeypatch.setattr(config, "llm_force_primary_fail", True)
    calls = _script(monkeypatch, [
        _resp(_tool("search_policy", {"query": "markup"}, "t1")),
        _resp(_respond("The foreign currency markup is 3.5%.")),
    ])
    out = agent.run_agent("CUST-2007", "markup?")
    assert out["status"] == "answered" and out["meta"]["fallback_used"]
    assert {c["model"] for c in calls} == {"grok-4-fast-non-reasoning"}
    assert out["meta"]["provider"] == "grok"


def test_rate_limited_primary_fails_over_without_retry(monkeypatch):
    from src.modules.llm.errors import ProviderRateLimited

    calls = _script(monkeypatch, [
        ProviderRateLimited("429 quota"),                         # gemini: limit reached -> straight to grok
        _resp(_respond("The foreign currency markup is 3.5%.")),
    ])
    out = agent.run_agent("CUST-2007", "markup?")
    assert [c["model"] for c in calls] == ["gemini-2.5-flash", "grok-4-fast-non-reasoning"]
    assert out["status"] == "answered" and "fallback_used" in out["meta"]["flags"]


def test_bad_key_opens_breaker_for_ten_minutes(monkeypatch):
    from src.modules.llm.errors import ProviderError

    _script(monkeypatch, [ProviderError("401", status=401, permanent=True), _resp(_respond("The foreign currency markup is 3.5%."))])
    agent.run_agent("CUST-2007", "markup?")
    assert llm_router.breaker("gemini-2.5-flash").is_open


def test_pii_is_masked_before_the_model_sees_it(monkeypatch):
    calls = _script(monkeypatch, [_resp(_respond("The foreign currency markup is 3.5%."))])
    agent.run_agent("CUST-2007", "My card 4111 1111 1111 1111 was charged a markup, OTP 482913. What is it?")
    first_user_message = calls[0]["messages"][0]["content"]
    assert "4111" not in first_user_message and "482913" not in first_user_message
    assert "[CARD_NUMBER]" in first_user_message


def test_customer_id_from_model_is_ignored(monkeypatch):
    _script(monkeypatch, [
        _resp(_tool("get_account_state", {"customer_id": "CUST-2003"}, "t1")),  # model tries another customer
        _resp(_tool("respond", {"status": "answered", "language": "en",
                                "claims": [{"id": "c1", "type": "account_fact", "text": "Your current limit is 50,000.",
                                            "citations": [{"kind": "account", "field": "current_limit"}]}],
                                "decision": {"outcome": "none", "reason_code": None}, "routing": None,
                                "unanswered": [], "conflicts_noted": []}, "r1")),
    ])
    out = agent.run_agent("CUST-2007", "What's my limit?")  # CUST-2007 limit = 50,000; CUST-2003 = 2,50,000
    assert out["status"] == "answered" and "50,000" in out["customer_message"]


def test_plain_text_answer_is_nudged_to_respond(monkeypatch):
    calls = _script(monkeypatch, [
        _resp(NS(type="text", text="The markup is 3.5%."), stop="end_turn"),
        _resp(_tool("search_policy", {"query": "markup"}, "t1")),
        _resp(_respond("The foreign currency markup is 3.5%.")),
    ])
    out = agent.run_agent("CUST-2007", "markup?")
    assert out["status"] == "answered" and calls[1]["messages"][-1]["content"] == agent.NUDGE


def test_short_rate_limit_waits_and_retries_same_model(monkeypatch):
    """Groq free tier: 8k tokens/min -> 429 'try again in 2s'. Waiting beats falling to static mode."""
    from src.modules.llm.errors import ProviderRateLimited

    waits = []
    monkeypatch.setattr(llm_router, "sleep", waits.append)
    calls = _script(monkeypatch, [ProviderRateLimited("429", retry_after=2.0), _resp(_respond("The foreign currency markup is 3.5%."))])
    out = agent.run_agent("CUST-2007", "markup?")
    assert [c["model"] for c in calls] == ["gemini-2.5-flash", "gemini-2.5-flash"] and waits and waits[0] >= 2.0
    assert out["status"] == "answered" and "rate_limit_wait" in out["meta"]["flags"]
