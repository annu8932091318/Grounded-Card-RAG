"""Entry scope guard (tier 2): off-domain only when BOTH corpus-derived signals are low; fails open."""
import pytest

from src.const.config import config
from src.modules.guardrails import scope_guard

MODEL = "local:BAAI/bge-small-en-v1.5"
VOCAB = {"late": 1, "payment": 1, "fee": 1, "card": 1, "insurance": 1, "markup": 1, "foreign": 1, "currency": 1}


@pytest.fixture
def fake_store(monkeypatch):
    from src.db.repositories import policy_repo
    from src.modules.retrieval import embedder, keyword_search

    monkeypatch.setattr(config, "scope_guard_enabled", True)
    monkeypatch.setattr(config, "retrieval_gate_model", MODEL)
    monkeypatch.setattr(config, "scope_thresholds", {MODEL: (0.475, 0.25)})
    monkeypatch.setattr(policy_repo, "searchable_models", lambda tenant: [MODEL])
    monkeypatch.setattr(embedder, "embed_query", lambda q, m: [0.0])
    monkeypatch.setattr(keyword_search, "_get_index", lambda tenant: {"df": VOCAB})
    sims = {}
    monkeypatch.setattr(policy_repo, "vector_search", lambda v, m, t, k: [{"sim": s} for s in sims["value"]])
    return sims


@pytest.mark.parametrize("question,top3,in_scope", [
    ("what's the weather tomorrow", [0.45, 0.44, 0.43], False),       # low similarity, no corpus words -> off-domain
    ("write me a poem about the sea", [0.42, 0.41, 0.40], False),
    ("what is the late payment fee", [0.70, 0.66, 0.60], True),        # covered
    ("can i add travel insurance to my card", [0.52, 0.50, 0.48], True),  # in domain, uncovered -> normal path
    ("give me stock market tips", [0.56, 0.55, 0.53], True),           # similar enough -> safe default (normal path)
    ("insurance tomorrow weather", [0.40, 0.40, 0.40], True),          # low similarity but corpus words -> in scope
])
def test_two_signal_rule(fake_store, question, top3, in_scope):
    fake_store["value"] = top3
    assert scope_guard.check(question)["in_scope"] is in_scope


def test_fails_open_when_the_check_breaks(fake_store, monkeypatch):
    from src.db.repositories import policy_repo

    monkeypatch.setattr(policy_repo, "vector_search", lambda *a: (_ for _ in ()).throw(RuntimeError("chroma down")))
    assert scope_guard.check("what's the weather")["in_scope"] is True


def test_unknown_gate_model_skips_the_check(fake_store, monkeypatch):
    monkeypatch.setattr(config, "scope_thresholds", {})
    assert scope_guard.check("what's the weather")["reason"] == "disabled"


def test_agent_answers_off_domain_without_llm_or_ticket(fake_store, monkeypatch):
    from src.modules.agent import agent
    from src.modules.llm import gemini_client, grok_client

    fake_store["value"] = [0.40, 0.40, 0.40]
    monkeypatch.setattr(agent, "_conversation", lambda cid, h: ("conv", []))
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no LLM call for an off-domain question"))
    monkeypatch.setattr(gemini_client, "call", boom)
    monkeypatch.setattr(grok_client, "call", boom)
    out = agent.run_agent("CUST-2000", "write me a poem about the sea")
    assert out["status"] == "refused" and out["routing"] is None and out["meta"]["llm_calls"] == 0
    assert "out_of_scope" in out["meta"]["flags"] and "credit card" in out["customer_message"]


def test_follow_up_in_a_conversation_is_not_scope_checked(fake_store, monkeypatch):
    from src.modules.agent import agent

    fake_store["value"] = [0.40, 0.40, 0.40]
    assert agent._out_of_scope(None, "and for my classic card?", history=[{"role": "user", "content": "x"}]) is None
