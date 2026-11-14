"""Embedding fallback chain: gemini-embedding-2 -> gemini-embedding-001 -> local bge-small -> (None: BM25 only)."""
import pytest

from src.const.config import config
from src.modules.cache import lru
from src.modules.llm.errors import ProviderError, ProviderRateLimited
from src.modules.retrieval import embedder

REAL_EMBED_QUERY = embedder.embed_query  # the autouse fixture replaces it; the last test needs the real one
CHAIN = ["gemini-embedding-2", "gemini-embedding-001", "local:BAAI/bge-small-en-v1.5"]


@pytest.fixture(autouse=True)
def chain(monkeypatch):
    monkeypatch.setattr(config, "embedding_models", CHAIN)
    monkeypatch.setattr(config, "gemini_api_key", "test-key")
    embedder._breakers.clear()
    lru.clear("embedding")
    calls = []

    def fake_query(text, model):
        calls.append(model)
        behaviour = fake_query.plan.get(model, "ok")
        if isinstance(behaviour, Exception):
            raise behaviour
        return [1.0, 0.0] if model.startswith("gemini") else [0.0, 1.0]
    fake_query.plan = {}
    monkeypatch.setattr(embedder, "embed_query", fake_query)
    return fake_query, calls


def test_primary_answers(chain):
    fake, calls = chain
    assert embedder.query_vector("q")[0] == "gemini-embedding-2" and calls == ["gemini-embedding-2"]


def test_quota_on_primary_uses_second_gemini_model(chain):
    fake, calls = chain
    fake.plan = {"gemini-embedding-2": ProviderRateLimited("429")}
    assert embedder.query_vector("q")[0] == "gemini-embedding-001"
    assert embedder.breaker("gemini-embedding-2").is_open  # quota: don't hammer it on every question


def test_both_gemini_models_down_falls_back_to_local(chain):
    fake, calls = chain
    fake.plan = {"gemini-embedding-2": ProviderRateLimited("429"), "gemini-embedding-001": ProviderError("401", 401, permanent=True)}
    model, vec = embedder.query_vector("q")
    assert model == CHAIN[2] and vec == [0.0, 1.0]


def test_everything_down_returns_none_never_raises(chain):
    fake, calls = chain
    fake.plan = {m: ProviderRateLimited("429") for m in CHAIN}
    assert embedder.query_vector("q") == (None, None)


def test_only_models_whose_collection_holds_the_corpus(chain):
    fake, calls = chain
    assert embedder.query_vector("q", only=[CHAIN[2]])[0] == CHAIN[2] and calls == [CHAIN[2]]


def test_gemini_vectors_are_renormalised(monkeypatch):
    monkeypatch.setattr(config, "gemini_api_key", "k")
    lru.clear("embedding")
    monkeypatch.setattr(embedder, "_post", lambda model, method, body: {"embedding": {"values": [3.0, 4.0]}})
    assert REAL_EMBED_QUERY("x", "gemini-embedding-001") == pytest.approx([0.6, 0.8])  # 768-d Matryoshka output is not unit length


def test_ingest_prunes_collections_of_removed_models(tmp_path, monkeypatch):
    """Removing a model from EMBEDDING_MODEL (all-MiniLM-L6-v2 -> bge-small) must not leave an orphan collection,
    and a quarantine collection embedded with the old local model is rebuilt."""
    from src.db import chroma

    monkeypatch.setattr(config, "chroma_path", tmp_path)
    chroma.reset_client()
    try:
        old = "local:sentence-transformers/all-MiniLM-L6-v2"
        chroma.policy_collection(old)
        monkeypatch.setattr(config, "embedding_models", [old])
        chroma.quarantine_collection()                   # built with the old local model
        monkeypatch.setattr(config, "embedding_models", CHAIN)
        chroma.policy_collection(CHAIN[0])
        dropped = chroma.prune_collections()
        assert chroma.collection_name(old) in dropped and chroma.QUARANTINE_COLLECTION in dropped
        assert chroma.collection_name(CHAIN[0]) not in dropped
        assert chroma.prune_collections() == []          # idempotent
    finally:
        chroma.reset_client()
