"""Semantic answer cache: matching rules (exact / paraphrase / different numbers) and the per-request re-checks
the agent applies before serving a hit (no LLM call, citations re-retrieved, output guard for this customer)."""
import numpy as np
import pytest

from src.const.config import config
from src.modules.agent import agent
from src.modules.cache import semantic_cache
from src.modules.llm import gemini_client, grok_client

FEE_CHUNK = {"chunk_id": "c1", "doc_id": "POL-FEE-007", "section": "Table", "version": "5.1",
             "text": "POL-FEE-007 Fees and Charges Schedule §Table: | Foreign currency markup | 3.5% |",
             "scores": {"kw": 1.0, "vec": 0.6, "rrf": 0.03}}
ANSWER = {"status": "answered", "language": "en",
          "claims": [{"id": "c1", "type": "policy_fact", "text": "The foreign currency markup is 3.5%.",
                      "citations": [{"kind": "policy", "doc_id": "POL-FEE-007", "section": "Table", "quote": None}]}],
          "decision": {"outcome": "none", "reason_code": None}, "routing": None, "unanswered": [], "conflicts_noted": []}


def _vec(*weights) -> list[float]:
    v = np.zeros(384, dtype=np.float32)
    v[: len(weights)] = weights
    return (v / np.linalg.norm(v)).tolist()


# fake embedder: paraphrases of the markup question point the same way, other topics elsewhere
EMB = {
    "what is the foreign currency markup": _vec(1, 0.05),
    "how much markup do you charge on foreign currency spends": _vec(1, 0.08),
    "what is the late fee on a balance of 5000": _vec(0, 1),
    "what is the late fee on a balance of 50000": _vec(0, 1, 0.01),
    "how do i close my account": _vec(0, 0, 1),
}


class FakeRepo:
    def __init__(self):
        self.rows, self.hits = {}, []

    def get_exact(self, key):
        return self.rows.get(key)

    def candidates(self, tenant, pv, corpus, limit):
        return [r for r in self.rows.values() if (r["tenant_id"], r["prompt_version"], r["corpus_hash"]) == (tenant, pv, corpus)]

    def put(self, e):
        self.rows[e["cache_key"]] = dict(e)

    def record_hit(self, key):
        self.hits.append(key)


@pytest.fixture
def repo(monkeypatch):
    from src.db.repositories import semantic_cache_repo
    from src.modules.retrieval import embedder

    fake = FakeRepo()
    for name in ("get_exact", "candidates", "put", "record_hit"):
        monkeypatch.setattr(semantic_cache_repo, name, getattr(fake, name))
    monkeypatch.setattr(embedder, "embed_query", lambda q: EMB[q.rstrip("?.! ")])
    monkeypatch.setattr(semantic_cache, "corpus_hash", lambda tenant: "corpus-1")
    monkeypatch.setattr(config, "answer_cache_ttl_s", 3600)
    monkeypatch.setattr(config, "semantic_cache_min_sim", 0.87)
    return fake


def test_exact_and_paraphrase_hits(repo):
    semantic_cache.store("What is the foreign currency markup?", "default", "v5", ANSWER, "gemini-2.5-flash")
    exact = semantic_cache.lookup("what is the FOREIGN currency markup", "default", "v5")
    assert exact["match"] == "exact" and exact["cited_sections"] == ["POL-FEE-007|Table"]
    para = semantic_cache.lookup("How much markup do you charge on foreign currency spends?", "default", "v5")
    assert para["match"] == "semantic" and para["similarity"] >= 0.95


def test_no_hit_for_other_topic_prompt_or_numbers(repo):
    semantic_cache.store("What is the foreign currency markup?", "default", "v5", ANSWER, None)
    semantic_cache.store("What is the late fee on a balance of 5,000?", "default", "v5", ANSWER, None)
    assert semantic_cache.lookup("How do I close my account?", "default", "v5") is None
    assert semantic_cache.lookup("What is the foreign currency markup?", "default", "v6") is None  # new prompt version
    # embeddings ~0.99 similar, but 5000 != 50000 -> must not reuse the answer
    assert semantic_cache.lookup("What is the late fee on a balance of 50,000?", "default", "v5") is None


def _offline_agent(monkeypatch, chunks):
    monkeypatch.setattr(agent.retriever, "search", lambda q, t, k=None: {
        "query": q, "grounded": bool(chunks), "top_vec_sim": 0.6, "chunks": chunks, "near_misses": [], "conflicts": [],
        "cache_hit": False})
    monkeypatch.setattr(agent, "_conversation", lambda cid, h: ("conv", []))
    calls = []

    def no_llm(*a, **k):
        calls.append(a)
        raise AssertionError("the LLM must not be called on a cache hit")
    monkeypatch.setattr(gemini_client, "call", no_llm)
    monkeypatch.setattr(grok_client, "call", no_llm)
    return calls


def test_agent_serves_hit_without_llm(monkeypatch, repo):
    semantic_cache.store("What is the foreign currency markup?", "default", "v5", ANSWER, "gemini-2.5-flash")
    calls = _offline_agent(monkeypatch, [FEE_CHUNK])
    out = agent.run_agent("CUST-2007", "How much markup do you charge on foreign currency spends?")
    assert out["status"] == "answered" and out["meta"]["cache_hit"] and out["meta"]["llm_calls"] == 0 and not calls
    assert "cache_hit_semantic" in out["meta"]["flags"] and repo.hits


def test_hit_rejected_when_citations_not_retrieved_again(monkeypatch, repo):
    semantic_cache.store("What is the foreign currency markup?", "default", "v5", ANSWER, None)
    other = {**FEE_CHUNK, "doc_id": "POL-SVC-006", "section": "body"}
    _offline_agent(monkeypatch, [other])
    monkeypatch.setattr(config, "llm_disabled", True)  # -> a miss falls through to static mode (no LLM)
    out = agent.run_agent("CUST-2007", "What is the foreign currency markup?")
    assert not out["meta"]["cache_hit"] and "static_mode" in out["meta"]["flags"]


def test_hit_rejected_for_vulnerable_customer_when_answer_talks_offers(monkeypatch, repo):
    offer = {**ANSWER, "claims": [{**ANSWER["claims"][0], "text": "You could upgrade to the Signature card for a 3.5% markup."}]}
    semantic_cache.store("What is the foreign currency markup?", "default", "v5", offer, None)
    _offline_agent(monkeypatch, [FEE_CHUNK])
    monkeypatch.setattr(config, "llm_disabled", True)
    out = agent.run_agent("CUST-2006", "What is the foreign currency markup?")  # CUST-2006 is vulnerable
    assert not out["meta"]["cache_hit"]


def test_unknown_customer_is_still_404_on_a_hit(monkeypatch, repo):
    from src.modules.tools.errors import CustomerNotFound

    semantic_cache.store("What is the foreign currency markup?", "default", "v5", ANSWER, None)
    _offline_agent(monkeypatch, [FEE_CHUNK])
    with pytest.raises(CustomerNotFound):
        agent.run_agent("CUST-9999", "What is the foreign currency markup?")
