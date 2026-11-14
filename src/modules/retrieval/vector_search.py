"""Vector leg: ChromaDB cosine similarity over an HNSW index; tenant + authority filters in the `where` clause."""
from src.db.repositories import policy_repo


def search(query_vec: list[float], model: str, tenant: str, limit: int) -> list[dict]:
    """Searches the collection of the model that produced query_vec (vectors of different models never mix)."""
    return policy_repo.vector_search(query_vec, model, tenant, limit)
