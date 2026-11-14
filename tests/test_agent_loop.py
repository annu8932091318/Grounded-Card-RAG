"""The agent loop with a scripted fake model (no API key, no DB): happy path, repair, safe fallback,
static mode, model fallback, customer binding."""
from types import SimpleNamespace as NS

import pytest

from src.const.config import config
from src.modules.agent import agent
from src.modules.llm import anthropic_client, llm_router

FEE_CHUNK = {"chunk_id": 1, "doc_id": "POL-FEE-007", "section": "Table", "version": "5.1",
             "text": "POL-FEE-007 Fees and Charges Schedule §Table: | Foreign currency markup | 3.5% |",
             "scores": {"kw": 0.2, "vec": 0.24, "rrf": 0.03}}


def _resp(*blocks, stop="tool_use"):
    return NS(content=list(blocks), stop_reason=stop, model="claude-sonnet-5-5",
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
    from src.db.repositories import service_request_repo as sr
    monkeypatch.setattr(sr, "find_recent", lambda k: None)
    monkeypatch.setattr(sr, "insert_pending", lambda *a: None)
    monkeypatch.setattr(sr, "mark", lambda *a: None)
    from src.db.repositories import trace_repo
    monkeypatch.setattr(trace_repo, "save", lambda *a: None)
    monkeypatch.setattr(config, "answer_cache_ttl_s", 0)
    monkeypatch.setattr(config, "llm_force_primary_fail", False)
    monkeypatch.setattr(config, "llm_disabled", False)
    llm_router.primary_breaker.success()


def _script(monkeypatch, responses):
    calls = []

    def fake_call(model, system, tools, messages):
        calls.append({"model": model, "messages": list(messages)})
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(anthropic_client, "call", fake_call)
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
    import anthropic
    import httpx
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    _script(monkeypatch, [err] * 4)
    monkeypatch.setattr(llm_router, "sleep", lambda s: None)
    out = agent.run_agent("CUST-2007", "foreign currency markup?")
    assert out["status"] == "degraded" and "static_mode" in out["meta"]["flags"]
    assert any(c.get("doc_id") == "POL-FEE-007" for c in out["citations_flat"])


def test_primary_outage_uses_fallback_model(monkeypatch):
    monkeypatch.setattr(config, "llm_force_primary_fail", True)
    calls = _script(monkeypatch, [
        _resp(_tool("search_policy", {"query": "markup"}, "t1")),
        _resp(_respond("The foreign currency markup is 3.5%.")),
    ])
    out = agent.run_agent("CUST-2007", "markup?")
    assert out["status"] == "answered" and out["meta"]["fallback_used"]
    assert {c["model"] for c in calls} == {config.fallback_model}


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
