"""Keyword leg: BM25 (Okapi, k1=1.5, b=0.75) computed in memory over the searchable chunks in ChromaDB.

WHY in memory: the corpus is ~30 chunks; an index this size builds in milliseconds and is rebuilt only when
ingest changes the corpus (clear_index). Chroma itself has no BM25. At thousands of documents this moves to a
real search engine (OpenSearch / Postgres FTS) — declared in the README.

Tokens are matched on text_norm (numbers normalised: "4,00,000" -> "400000", "3.5%" -> "3.5 percent"), plus the
doc id, so "POL-FEE-007" and "400000" both match.
"""
import math
import re
import threading
from collections import Counter

from src.db.repositories import policy_repo

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "can", "could", "did", "do", "does", "for", "from", "have",
    "how", "i", "if", "in", "is", "it", "me", "my", "of", "on", "or", "please", "the", "to", "was", "what",
    "when", "where", "which", "why", "will", "with", "would", "you", "your", "this", "that", "there", "get",
}
# Words that appear in almost every chunk — a keyword match on these alone is not evidence of coverage.
GENERIC_TERMS = {"card", "credit", "customer", "customers", "account", "bank", "limit", "policy", "pol"}
_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
K1, B = 1.5, 0.75

_index: dict[str, dict] = {}
_lock = threading.Lock()


def tokens(text_norm: str) -> list[str]:
    return _TOKEN.findall(text_norm.lower())


def terms(q_norm: str) -> list[str]:
    """Query terms: content words, deduplicated, order kept."""
    return list(dict.fromkeys(t for t in tokens(q_norm) if t not in _STOPWORDS))


def coverage(q_norm: str, kw_rows: list[dict]) -> float:
    """Share of the question's specific terms (>= 3 letters, not generic) found in the best keyword hit.
    WHY not 'any term matches' (the old rule): measured, it grounded 6 of 11 out-of-corpus questions — the token
    's' from "what's" or 'rate' alone matched some policy. Coverage of the question's own terms is much stricter."""
    wanted = {t for t in terms(q_norm) if len(t) >= 3} - GENERIC_TERMS
    if not kw_rows or not wanted:
        return 0.0
    return len(wanted & set(tokens(kw_rows[0]["text_norm"]))) / len(wanted)


def specific_hit(q_norm: str, kw_rows: list[dict], min_coverage: float = 0.34) -> bool:
    return coverage(q_norm, kw_rows) >= min_coverage


def build_index(rows: list[dict]) -> dict:
    docs = [Counter(tokens(r["text_norm"] + " " + r["doc_id"])) for r in rows]
    lengths = [sum(d.values()) for d in docs]
    df = Counter(t for d in docs for t in d)
    return {"rows": rows, "tf": docs, "len": lengths, "df": df, "n": len(rows),
            "avgdl": (sum(lengths) / len(lengths)) if lengths else 0.0}


def bm25(index: dict, q_terms: list[str], limit: int) -> list[dict]:
    n, avgdl, scored = index["n"], index["avgdl"] or 1.0, []
    for row, tf, dl in zip(index["rows"], index["tf"], index["len"], strict=True):
        score = 0.0
        for t in q_terms:
            f = tf.get(t, 0)
            if f:
                idf = math.log(1 + (n - index["df"][t] + 0.5) / (index["df"][t] + 0.5))
                score += idf * f * (K1 + 1) / (f + K1 * (1 - B + B * dl / avgdl))
        if score > 0:
            scored.append({**row, "kw": score})
    return sorted(scored, key=lambda r: r["kw"], reverse=True)[:limit]


def _get_index(tenant: str) -> dict:
    with _lock:
        if tenant not in _index:
            _index[tenant] = build_index(policy_repo.all_chunks(tenant))
        return _index[tenant]


def clear_index() -> None:
    """Called by ingest: the corpus changed."""
    with _lock:
        _index.clear()


def search(q_norm: str, tenant: str, limit: int) -> list[dict]:
    q = terms(q_norm)
    return bm25(_get_index(tenant), q, limit) if q else []
