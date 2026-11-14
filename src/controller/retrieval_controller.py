"""Debug endpoint: hybrid search with per-leg scores (used for the 'retrieval miss' demo)."""
from src.middleware.validate_middleware import SearchRequest
from src.modules.retrieval import retriever


def search(body: SearchRequest, ctx: dict) -> dict:
    result = retriever.search(body.query, ctx["tenant_id"], body.top_k)
    return {
        "query": result["query"],
        "grounded": result["grounded"],
        "top_vec_sim": result["top_vec_sim"],
        "embedding_model": result.get("embedding_model"),
        "gate": result.get("gate"),
        "chunks": [{k: c[k] for k in ("doc_id", "section", "text", "scores")}
                   for c in (result["chunks"] or result["near_misses"])],
        "conflicts": result["conflicts"],
    }
