"""ENTRY guardrail, tier 2: is the question about this service at all? Decided ONLY from the provided policy documents
(their vectors and their vocabulary) — no hand-written topic or keyword list.

Two corpus-derived signals, both local and quota-free:
  * semantic closeness: mean cosine of the question's 3 nearest policy chunks, embedded with the local gate model
    (bge-small) — the same collection the relevance gate uses
  * vocabulary overlap: share of the question's content words (>= 3 letters, not stopwords) that occur anywhere in
    the policy documents (the BM25 index built from them)
Off-domain only if BOTH are low. Measured (2026-11-16) on 24 covered + 16 in-domain-but-uncovered + 16 off-domain probes:
  top-3 sim  covered 0.56-0.78 | uncovered 0.49-0.66 | off-domain 0.39-0.55
  corpus-word share  covered 0.25-1.0 | uncovered 0.0-1.0 | off-domain 0.0-0.2
  rule sim < 0.475 AND share < 0.25 -> off-domain caught 14/16, in-domain wrongly blocked 0/40, margin 0.074.
Errors may only go the safe way: a missed off-domain question gets the normal path (honest gap + human ticket).

Tiers:  in scope + covered -> agent | in domain, not covered -> agent (honest gap + GENERAL ticket, spec D3)
        off-domain -> fixed reply here: no LLM call, no retrieval for the answer, no ticket.
Fails open: if the embedding model or the vector store is unavailable, the question is treated as in scope.
"""
from src.const.config import config
from src.modules.telemetry.logger import log

MIN_WORDS_FOR_VOCAB = 1


def check(question_norm: str, tenant: str = "default") -> dict:
    """-> {"in_scope": bool, "sim": float|None, "corpus_word_share": float|None, "model": str|None, "reason": str}"""
    model = config.retrieval_gate_model
    limits = config.scope_thresholds.get(model)
    if not config.scope_guard_enabled or limits is None:
        return {"in_scope": True, "sim": None, "corpus_word_share": None, "model": model, "reason": "disabled"}
    try:
        from src.db.repositories import policy_repo
        from src.modules.retrieval import embedder, keyword_search

        if model not in policy_repo.searchable_models(tenant):
            return {"in_scope": True, "sim": None, "corpus_word_share": None, "model": model, "reason": "no_corpus"}
        hits = policy_repo.vector_search(embedder.embed_query(question_norm, model), model, tenant, 3)
        sim = round(sum(h["sim"] for h in hits) / len(hits), 4) if hits else 0.0
        vocabulary = keyword_search._get_index(tenant)["df"]
        words = [t for t in keyword_search.terms(question_norm) if len(t) >= 3]
        share = round(sum(t in vocabulary for t in words) / len(words), 3) if len(words) >= MIN_WORDS_FOR_VOCAB else 0.0
    except Exception as e:  # never block a customer because the check itself failed
        log("scope_check_failed", level="warning", error=type(e).__name__, detail=str(e)[:160])
        return {"in_scope": True, "sim": None, "corpus_word_share": None, "model": model, "reason": "check_failed"}
    max_sim, max_share = limits
    off = sim < max_sim and share < max_share
    return {"in_scope": not off, "sim": sim, "corpus_word_share": share, "model": model,
            "reason": "off_domain" if off else "in_scope"}
