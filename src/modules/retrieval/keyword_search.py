"""Keyword leg: Postgres full-text ('simple' config) ranked by ts_rank_cd. Not true BM25 — declared limitation."""
import re

from src.db.repositories import policy_repo

# WHY a small stopword list + OR-query: websearch_to_tsquery ANDs every word, so a natural question
# ("what is the markup if I use my card in Singapore") would match nothing.
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "can", "could", "did", "do", "does", "for", "from", "have",
    "how", "i", "if", "in", "is", "it", "me", "my", "of", "on", "or", "please", "the", "to", "was", "what",
    "when", "where", "which", "why", "will", "with", "would", "you", "your", "this", "that", "there", "get",
}


# Words that appear in almost every chunk — a keyword match on these alone is not evidence of coverage.
GENERIC_TERMS = {"card", "credit", "customer", "customers", "account", "bank", "limit", "policy", "pol"}


def terms(q_norm: str) -> list[str]:
    tokens = [t for t in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", q_norm) if t not in _STOPWORDS]
    return list(dict.fromkeys(tokens))  # dedupe, keep order


def to_tsquery(q_norm: str) -> str:
    return " | ".join(f"'{t}'" for t in terms(q_norm))


def specific_hit(q_norm: str, kw_rows: list[dict]) -> bool:
    """True if the best keyword hit contains at least one non-generic query term."""
    wanted = set(terms(q_norm)) - GENERIC_TERMS
    if not kw_rows or not wanted:
        return False
    top_words = set(re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", kw_rows[0]["text_norm"]))
    return bool(wanted & top_words)


def search(q_norm: str, tenant: str, limit: int) -> list[dict]:
    return policy_repo.keyword_search(to_tsquery(q_norm), tenant, limit)
