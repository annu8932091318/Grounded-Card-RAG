"""Semantic answer cache (Postgres table semantic_cache): answer a repeated or paraphrased question from the
database instead of calling the LLM.

Lookup, cheapest first:
  1. exact   : cache_key = sha256(tenant | prompt_version | corpus_hash | normalised question)  -> one PK read
  2. semantic: cosine(question embedding, stored embeddings of the SAME embedding model) >= SEMANTIC_CACHE_MIN_SIM[model]
               (gemini-embedding-2: 0.89 — measured paraphrases 0.90-0.94, same topic + different detail 0.86,
               other fees ~0.71; local bge-small: 0.90 — its scale is different, so each model has its own threshold)
               AND the numbers in the two questions are identical ("late fee on 5,000" != "late fee on 50,000",
               although their embeddings are ~0.98 similar)
A candidate is only *served* after agent.py re-checks it for THIS request (see agent._serve_from_cache):
  * every policy section the answer cites is still among fresh retrieval hits for the new question
    (local BM25 + vector search, no LLM) — the answer is still grounded
  * the output guard passes for the asking customer (e.g. no product talk for a vulnerable customer)

What is stored: only account-independent answers (no account data, no service request, no input flags, no
conversation history, no degraded path) — exactly agent._cacheable(). Scope keys make stale entries
unreachable: a new prompt version or a re-ingested corpus starts a fresh cache; ingest also deletes them.

Why not the embedding itself as the primary key: two different float vectors never compare equal, so an
embedding PK would only ever match the identical question. The hash key gives the exact fast path; the stored
embedding gives the semantic path.

Scale-out: the cosine scan is numpy over <= SEMANTIC_CACHE_MAX_CANDIDATES rows. Multi-node / large caches ->
Redis (RediSearch HNSW vector index, TTL per key, shared by all API workers) or pgvector. See README.
"""
import hashlib
import re
import threading

import numpy as np

from src.const.config import config, threshold
from src.helper.text import extract_numbers, normalise
from src.modules.telemetry.logger import log

_corpus_hash: dict[str, str] = {}
_lock = threading.Lock()


def enabled() -> bool:
    return config.answer_cache_ttl_s > 0


def corpus_hash(tenant: str) -> str:
    with _lock:
        if tenant not in _corpus_hash:
            from src.db.repositories import policy_repo

            _corpus_hash[tenant] = policy_repo.corpus_hash(tenant)
        return _corpus_hash[tenant]


def invalidate(tenant: str | None = None) -> int:
    """Called by ingest: forget the corpus hash and delete entries built on an older corpus."""
    with _lock:
        _corpus_hash.clear()
    try:
        from src.db.repositories import semantic_cache_repo

        return semantic_cache_repo.delete_stale(tenant or "default", corpus_hash(tenant or "default"))
    except Exception as e:
        log("semantic_cache_invalidate_failed", level="warning", error=type(e).__name__)
        return 0


def cache_key(tenant: str, prompt_version: str, corpus: str, q_norm: str) -> str:
    """Punctuation and spacing don't change the question: 'Markup?' and 'markup' share one key."""
    text = " ".join(re.sub(r"[^\w\s.%]", " ", q_norm).split()).strip(" .")
    return hashlib.sha256("|".join([tenant, prompt_version, corpus, text]).encode()).hexdigest()


def lookup(message: str, tenant: str, prompt_version: str) -> dict | None:
    """-> {"answer", "cache_key", "match": "exact"|"semantic", "similarity", "cited_sections", "model_used"} or None."""
    if not enabled():
        return None
    from src.db.repositories import semantic_cache_repo as repo
    from src.modules.retrieval import embedder

    q_norm = normalise(message)
    corpus = corpus_hash(tenant)
    exact = repo.get_exact(cache_key(tenant, prompt_version, corpus, q_norm))
    if exact:  # no embedding needed: works even when every embedding model is down
        return {**_hit(exact), "match": "exact", "similarity": 1.0}

    model, vec = embedder.query_vector(q_norm)  # same call (LRU-cached) as retrieval's vector leg
    if model is None:
        return None
    rows = repo.candidates(tenant, prompt_version, corpus, model, config.semantic_cache_max_candidates)
    if not rows:
        return None
    min_sim = threshold(config.semantic_cache_min_sim, model, 0.9)
    q = np.asarray(vec, dtype=np.float32)
    matrix = np.asarray([r["embedding"] for r in rows], dtype=np.float32)
    sims = matrix @ q  # vectors are L2-normalised: dot product == cosine similarity
    q_numbers = extract_numbers(q_norm)
    for i in np.argsort(-sims):
        sim = float(sims[i])
        if sim < min_sim:
            break
        if extract_numbers(rows[i]["question_norm"]) == q_numbers:
            return {**_hit(rows[i]), "match": "semantic", "similarity": round(sim, 4), "embedding_model": model}
    return None


def _hit(row: dict) -> dict:
    return {"answer": row["answer"], "cache_key": row["cache_key"], "cited_sections": list(row["cited_sections"] or []),
            "model_used": row.get("model_used"), "question_norm": row["question_norm"]}


def store(message: str, tenant: str, prompt_version: str, payload: dict, model_used: str | None) -> None:
    from src.db.repositories import semantic_cache_repo as repo
    from src.modules.retrieval import embedder

    q_norm = normalise(message)
    corpus = corpus_hash(tenant)
    model, vec = embedder.query_vector(q_norm)  # None -> stored for exact matches only
    cited = sorted({f"{c['doc_id']}|{c['section']}" for cl in payload["claims"] for c in cl["citations"]
                    if c.get("kind") == "policy"})
    repo.put({"cache_key": cache_key(tenant, prompt_version, corpus, q_norm), "tenant_id": tenant,
              "prompt_version": prompt_version, "corpus_hash": corpus, "question_norm": q_norm,
              "embedding": [float(v) for v in (vec or [])], "embedding_model": model or "none",
              "answer": payload, "cited_sections": cited,
              "model_used": model_used, "ttl_s": int(config.answer_cache_ttl_s)})
