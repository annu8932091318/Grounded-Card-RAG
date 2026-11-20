"""The agent loop with a scripted fake model (no API key, no DB): happy path, repair, safe fallback,
no-model 503, model fallback, customer binding."""
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
    monkeypatch.setattr(agent, "_conversation", lambda cid, h: (cid or "00000000-0000-0000-0000-00000000c0de", []))
    monkeypatch.setattr(agent, "_routing_policy_chunk", lambda turn: None)
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
        calls.append({"model": model, "messages": list(messages), "tools": [t["name"] for t in tools]})
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
    repair_msg = [b for b in calls[2]["messages"][-1]["content"] if b.get("type") == "tool_result"][-1]
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


def test_all_models_down_raises_limit_reached_with_the_trace(monkeypatch):
    """No static answer: no model -> LLMLimitReached (HTTP 503 LLM_LIMIT_REACHED, "try again tomorrow")."""
    err = ProviderRetryable("network down")
    _script(monkeypatch, [err] * 4)  # gemini: 2 attempts, grok: 2 attempts
    monkeypatch.setattr(llm_router, "sleep", lambda s: None)
    with pytest.raises(agent.LLMLimitReached) as exc:
        agent.run_agent("CUST-2007", "foreign currency markup?")
    internal = exc.value.internal
    assert internal["error_code"] == "LLM_LIMIT_REACHED" and internal["llm_failures"] == 4
    assert "llm_unavailable" in internal["flags"]


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


def test_coverage_gap_is_routed_to_general(make_turn):
    turn = make_turn("CUST-2009")
    turn.misses.append("travel insurance")
    raw = {"status": "answered_with_gap", "claims": [{"id": "c1", "type": "gap", "text": "Not covered.",
                                                      "citations": [{"kind": "retrieval_miss", "query": "travel insurance"}]}]}
    out = agent._complete_routing(raw, turn)
    assert out["routing"]["queue"] == "GENERAL" and out["claims"][-1]["id"] == "routing"


def test_unknown_customer_is_404_even_when_no_model_is_available(monkeypatch):
    """Regression (found in the live API test): with no LLM, CUST-9999 must still be a 404, not a quota message."""
    from src.modules.tools.errors import CustomerNotFound

    monkeypatch.setattr(config, "llm_disabled", True)
    with pytest.raises(CustomerNotFound):
        agent.run_agent("CUST-9999", "What is the markup?")


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
    assert out["status"] == "answered" and calls[1]["messages"][-1]["content"].startswith(agent.NUDGE)


def test_short_rate_limit_waits_and_retries_same_model(monkeypatch):
    """Groq free tier: 8k tokens/min -> 429 'try again in 2s'. Waiting beats returning the no-model 503."""
    from src.modules.llm.errors import ProviderRateLimited

    waits = []
    monkeypatch.setattr(llm_router, "sleep", waits.append)
    calls = _script(monkeypatch, [ProviderRateLimited("429", retry_after=2.0), _resp(_respond("The foreign currency markup is 3.5%."))])
    out = agent.run_agent("CUST-2007", "markup?")
    assert [c["model"] for c in calls] == ["gemini-2.5-flash", "gemini-2.5-flash"] and waits and waits[0] >= 2.0
    assert out["status"] == "answered" and "rate_limit_wait" in out["meta"]["flags"]


def test_rate_limit_waits_never_exceed_the_agent_budget(monkeypatch):
    """Regression (Postman run): two 12 s Retry-After waits blew the 30 s HTTP timeout -> 504 instead of a clean 503 LLM_LIMIT_REACHED."""
    from src.modules.llm.errors import ProviderRateLimited

    waits = []
    monkeypatch.setattr(llm_router, "sleep", waits.append)
    monkeypatch.setattr(config, "agent_timeout_s", 5.0)  # budget smaller than the 12 s the provider asks for
    _script(monkeypatch, [ProviderRateLimited("429", retry_after=12.0)] * 4)
    with pytest.raises(agent.LLMLimitReached):
        agent.run_agent("CUST-2007", "markup?")
    assert waits == []


# --- ReAct: failures come back to the model as observations, never crash the turn ------------------------------

def _flaky_search(monkeypatch, fail_times: int):
    """retriever.search that raises for the first `fail_times` calls (Chroma / embedding down), then works."""
    calls = []

    def search(q, t, k=None):
        calls.append(q)
        if len(calls) <= fail_times:
            raise ConnectionError("chroma unavailable")
        return {"query": q, "grounded": True, "top_vec_sim": 0.24, "chunks": [FEE_CHUNK], "near_misses": [],
                "conflicts": [], "cache_hit": False}
    monkeypatch.setattr(agent.retriever, "search", search)
    return calls


def test_retrieval_failure_is_an_observation_and_the_model_retries(monkeypatch):
    searches = _flaky_search(monkeypatch, fail_times=1)  # the up-front search fails, the model's retry works
    calls = _script(monkeypatch, [
        _resp(_tool("search_policy", {"query": "foreign currency markup"}, "t1")),
        _resp(_respond("The foreign currency markup is 3.5%.")),
    ])
    out = agent.run_agent("CUST-2007", "What is the foreign currency markup?")
    assert 'status="failed"' in str(calls[0]["messages"])  # the model was told the first search failed
    assert out["status"] == "answered" and len(searches) == 2
    assert "tool_error:search_policy" in out["meta"]["flags"]


def test_retrieval_down_for_the_whole_turn_never_crashes(monkeypatch):
    searches = _flaky_search(monkeypatch, fail_times=99)
    calls = _script(monkeypatch, [
        _resp(_tool("search_policy", {"query": "markup"}, "t1")),        # 2nd failure -> not retryable
        _resp(_tool("search_policy", {"query": "markup fee"}, "t2")),    # budget used up -> NOT executed
        _resp(_respond("The foreign currency markup is 3.5%.")),          # cites nothing retrieved -> invalid
        _resp(_respond("The foreign currency markup is 3.5%.", tid="r2")),
    ])
    out = agent.run_agent("CUST-2007", "What is the foreign currency markup?")
    assert len(searches) == 2  # up-front search + one retry; the third call never reached Chroma
    observation = calls[2]["messages"][-1]["content"][0]
    assert observation["is_error"] and "RetryBudgetExhausted" in observation["content"]
    assert out["status"] == "refused" and "3.5" not in out["customer_message"]  # safe response, nothing guessed


def test_crash_inside_a_tool_is_an_observation(monkeypatch):
    def broken_rules(raw, facts):
        raise KeyError("avg_utilisation_3m")
    monkeypatch.setattr(agent, "evaluate", broken_rules)
    calls = _script(monkeypatch, [
        _resp(_tool("check_limit_increase_eligibility", {}, "t1")),
        _resp(_respond("The foreign currency markup is 3.5%.")),
    ])
    out = agent.run_agent("CUST-2007", "markup?")
    observation = calls[1]["messages"][-1]["content"][0]
    assert observation["is_error"] and '"retryable": true' in observation["content"]
    assert out["status"] == "answered" and "tool_error:check_limit_increase_eligibility" in out["meta"]["flags"]


def test_failed_service_request_is_never_retried(monkeypatch):
    attempts = []

    def broken_sr(turn, category, summary):
        attempts.append(category)
        raise RuntimeError("queue API returned garbage")
    monkeypatch.setattr(agent, "run_raise_service_request", broken_sr)
    calls = _script(monkeypatch, [
        _resp(_tool("raise_service_request", {"category": "CARDS_URGENT", "summary": "block card"}, "t1")),
        _resp(_tool("raise_service_request", {"category": "CARDS_URGENT", "summary": "block card"}, "t2")),
        _resp(_respond("The foreign currency markup is 3.5%.")),
    ])
    agent.run_agent("CUST-2007", "markup?")
    assert attempts == ["CARDS_URGENT"]  # the model's second attempt was answered from the budget, not executed
    assert "couldn't confirm" in calls[1]["messages"][-1]["content"][0]["content"]


# --- ReAct: Reason -> Act -> Observe, with the loop state visible to the agent and a forced finish ----------------

def test_each_step_records_thought_actions_observations_and_outcome(monkeypatch):
    calls = _script(monkeypatch, [
        _resp(NS(type="text", text="Thought: I need the markup from the fee schedule."),
              _tool("search_policy", {"query": "foreign currency markup"}, "t1")),
        _resp(_respond("The foreign currency markup is 3.5%.")),
    ])
    out = agent.run_agent("CUST-2007", "What is the foreign currency markup?")
    steps = [s for s in out["_internal"]["spans"] if s["name"] == "agent.step"]
    assert [s["attrs"]["step"] for s in steps] == [1, 2]
    assert steps[0]["attrs"]["thought"].startswith("Thought: I need the markup")
    assert steps[0]["attrs"]["actions"] == ["search_policy"] and steps[0]["attrs"]["outcome"] == "observed"
    assert steps[0]["attrs"]["observations"] == [{"tool": "search_policy", "ok": True}]
    assert steps[1]["attrs"]["outcome"] == "finished"
    assert "Thought:" not in out["customer_message"]  # the reasoning never reaches the customer
    assert '<loop_state step="1" max_steps="6"' in str(calls[1]["messages"][-1]["content"])  # the agent sees its budget


def test_parallel_actions_in_one_step_are_all_observed(monkeypatch):
    calls = _script(monkeypatch, [
        _resp(_tool("get_account_state", {}, "t1"), _tool("search_policy", {"query": "markup"}, "t2")),
        _resp(_respond("The foreign currency markup is 3.5%.")),
    ])
    agent.run_agent("CUST-2007", "markup?")
    observed = [b["tool_use_id"] for b in calls[1]["messages"][-1]["content"] if b.get("type") == "tool_result"]
    assert observed == ["t1", "t2"]


def test_last_step_offers_only_respond_forced_finish(monkeypatch):
    monkeypatch.setattr(config, "agent_max_iterations", 2)
    calls = _script(monkeypatch, [
        _resp(_tool("search_policy", {"query": "markup"}, "t1")),
        _resp(_respond("The foreign currency markup is 3.5%.")),
    ])
    out = agent.run_agent("CUST-2007", "markup?")
    assert "search_policy" in calls[0]["tools"] and calls[1]["tools"] == ["respond"]
    assert "LAST" in str(calls[1]["messages"][-1]["content"]) and out["status"] == "answered"


def test_prefetch_off_is_pure_react(monkeypatch):
    monkeypatch.setattr(config, "agent_prefetch_search", False)
    calls = _script(monkeypatch, [
        _resp(_tool("search_policy", {"query": "markup"}, "t1")),
        _resp(_respond("The foreign currency markup is 3.5%.")),
    ])
    out = agent.run_agent("CUST-2007", "markup?")
    assert "initial_policy_search" not in str(calls[0]["messages"]) and out["status"] == "answered"


def test_thought_argument_is_recorded_and_stripped_before_tools_and_validation(monkeypatch):
    """Every tool schema requires `thought` (ReAct REASON). It reaches the trace, never the tool or the validator."""
    from src.modules.agent.tools_schema import TOOLS

    assert all("thought" in t["input_schema"]["required"] for t in TOOLS)
    seen = []
    real = agent.retriever.search
    monkeypatch.setattr(agent.retriever, "search", lambda q, t, k=None: (seen.append(q), real(q, t))[1])
    answer = _respond("The foreign currency markup is 3.5%.")
    answer.input["thought"] = "I have the fee table excerpt; I can answer."  # respond schema is strict: must be stripped
    _script(monkeypatch, [
        _resp(_tool("search_policy", {"thought": "Need the markup row of the fee schedule.", "query": "markup"}, "t1")),
        _resp(answer),
    ])
    out = agent.run_agent("CUST-2007", "markup?")
    assert out["status"] == "answered" and "markup" in seen
    steps = [s["attrs"] for s in out["_internal"]["spans"] if s["name"] == "agent.step"]
    assert steps[0]["thought"] == "[search_policy] Need the markup row of the fee schedule."
    assert steps[1]["thought"] == "[respond] I have the fee table excerpt; I can answer."
    assert "fee schedule" not in out["customer_message"]
