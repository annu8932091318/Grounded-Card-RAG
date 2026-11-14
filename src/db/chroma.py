"""ChromaDB in embedded mode: the vector store is a folder on disk (CHROMA_PATH, default ./chroma_data).
No server, no Docker. Open the same folder with tools/chroma_peek to see what was embedded.

Collections (one per embedding model in EMBEDDING_MODEL, same chunk ids in each):
  policy_chunks__gemini_embedding_001        primary (Gemini API, 768-d)
  policy_chunks__gemini_embedding_2          fallback (Gemini API, 768-d)
  policy_chunks__local_BAAI_bge_small_en_v1_5  last resort (local CPU, 384-d)
  Collections of models no longer in the chain are dropped by prune_collections() at ingest.
  policy_quarantine     documents the ingest scan rejected (POL-OPS-012), embedded locally. Audit only, never searched.
WHY one collection per embedding model: vectors from different models are not comparable, so each model needs its
own index. WHY a separate quarantine collection instead of a flag: a forgotten filter can leak a flagged row; a
collection the search code never opens cannot. (Searchable rows also carry authority='policy': defence in depth.)
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


def collection_name(model: str) -> str:
    from src.modules.retrieval.embedder import collection_suffix

    return f"{config.chroma_collection}__{collection_suffix(model)}"


def _collection(name: str, model: str, description: str):
    # cosine space; we always pass our own (L2-normalised) Gemini embeddings — Chroma never embeds anything itself.
    from src.modules.retrieval.embedder import is_local

    return client().get_or_create_collection(
        name=name, embedding_function=None,
        metadata={"hnsw:space": "cosine", "embedding_model": model,
                  "embedding_dim": 384 if is_local(model) else config.embedding_dim, "description": description},
    )


def policy_collection(model: str | None = None):
    model = model or config.embedding_models[0]
    return _collection(collection_name(model), model, f"Policy corpus chunks (one per section), embedded with {model}")


def quarantine_collection():
    from src.modules.retrieval.embedder import local_fallback

    model = local_fallback()  # never searched -> never worth API quota, and never fails for lack of it
    return _collection(QUARANTINE_COLLECTION, model, "Quarantined documents (non-policy / injection). Never searched.")


def prune_collections() -> list[str]:
    """Drop what the current chain can no longer use: policy collections of models removed from EMBEDDING_MODEL
    (e.g. the old all-MiniLM-L6-v2), and the quarantine collection if it was embedded with another local model
    (ingest then re-embeds the quarantined doc). Returns the names dropped."""
    from src.modules.retrieval.embedder import local_fallback

    wanted = {collection_name(m) for m in config.embedding_models}
    prefix = f"{config.chroma_collection}__"
    dropped = []
    for col in client().list_collections():
        name = col if isinstance(col, str) else col.name  # chromadb >= 0.6 returns names, older returns objects
        if name.startswith(prefix) and name not in wanted:
            client().delete_collection(name)
            dropped.append(name)
        elif name == QUARANTINE_COLLECTION:
            meta = client().get_collection(name).metadata or {}
            if meta.get("embedding_model") != local_fallback():
                client().delete_collection(name)
                dropped.append(name)
    return dropped


def is_up() -> bool:
    try:
        client().list_collections()
        return True
    except Exception:
        return False


def reset_client() -> None:
    """Tests / tools: forget the cached client (e.g. after changing CHROMA_PATH)."""
    global _client
    with _lock:
        _client = None
