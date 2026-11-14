"""Vector leg: ChromaDB cosine similarity over an HNSW index; tenant + authority filters in the `where` clause."""
from src.db.repositories import policy_repo


def search(query_vec: list[float], tenant: str, limit: int) -> list[dict]:
    return policy_repo.vector_search(query_vec, tenant, limit)
