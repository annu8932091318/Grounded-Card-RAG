"""Vector leg: pgvector cosine distance over an HNSW index; tenant + quarantine filters in SQL."""
from src.db.repositories import policy_repo


def search(query_vec: list[float], tenant: str, limit: int) -> list[dict]:
    return policy_repo.vector_search(query_vec, tenant, limit)
