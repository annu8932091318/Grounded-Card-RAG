"""ChromaDB in embedded mode: the vector store is a folder on disk (CHROMA_PATH, default ./chroma_data).
No server, no Docker. Open the same folder with tools/chroma_peek to see what was embedded.

Two collections, on purpose:
  policy_chunks      searchable governed policy (the ONLY collection retrieval ever queries)
  policy_quarantine  documents the ingest scan rejected (POL-OPS-012). Kept for audit, never searchable.
WHY a separate collection instead of a `quarantined` flag: a forgotten filter can leak a flagged row; a
collection the search code never opens cannot. (The searchable rows still carry authority='policy' and the
search filters on it too: defence in depth.)
"""
import threading

from src.const.config import config

QUARANTINE_COLLECTION = "policy_quarantine"
_client = None
_lock = threading.Lock()


def client():
    global _client
    with _lock:
        if _client is None:
            import chromadb  # WHY lazy: unit tests and the HTTP layer import modules without touching the store
            from chromadb.config import Settings

            config.chroma_path.mkdir(parents=True, exist_ok=True)
            _client = chromadb.PersistentClient(path=str(config.chroma_path),
                                                settings=Settings(anonymized_telemetry=False))
        return _client


def _collection(name: str):
    # cosine space: our vectors are L2-normalised MiniLM embeddings, so distance = 1 - cosine similarity.
    # We always pass our own embeddings (fastembed, same all-MiniLM-L6-v2 model Chroma uses by default), so a
    # viewer that embeds query text with Chroma's default function gets compatible vectors.
    return client().get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine", "embedding_model": config.embedding_model,
                  "description": "Policy corpus chunks (one per section)" if name != QUARANTINE_COLLECTION
                  else "Quarantined documents (non-policy / injection). Never searched."},
    )


def policy_collection():
    return _collection(config.chroma_collection)


def quarantine_collection():
    return _collection(QUARANTINE_COLLECTION)


def is_up() -> bool:
    try:
        policy_collection().count()
        return True
    except Exception:
        return False


def reset_client() -> None:
    """Tests / tools: forget the cached client (e.g. after changing CHROMA_PATH)."""
    global _client
    with _lock:
        _client = None
