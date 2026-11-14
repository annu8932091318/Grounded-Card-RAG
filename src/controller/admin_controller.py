"""Admin: re-ingest the corpus (same code path as `python -m scripts.ingest`), ingest history, and a read-only
look into the ChromaDB collections (what was embedded, with which metadata) without opening a viewer."""
from src.db import chroma


def ingest(force: bool = False) -> dict:
    from scripts.ingest import run_ingest

    return run_ingest(force=force)


def ingest_runs(limit: int) -> dict:
    from src.db.repositories import ingest_repo

    return {"runs": ingest_repo.list_runs(min(max(limit, 1), 200))}


def vector_store(doc_id: str | None, limit: int) -> dict:
    from src.const.config import config

    out = {"chroma_path": str(config.chroma_path), "collections": []}
    for col in (chroma.policy_collection(), chroma.quarantine_collection()):
        got = col.get(where={"doc_id": doc_id} if doc_id else None, limit=min(max(limit, 1), 500),
                      include=["documents", "metadatas", "embeddings"])
        embeddings = got.get("embeddings")
        out["collections"].append({
            "name": col.name, "count": col.count(), "metadata": col.metadata,
            "items": [{
                "id": cid,
                "doc_id": meta["doc_id"], "section": meta["section"], "version": meta.get("version"),
                "authority": meta.get("authority"), "quarantine_reason": meta.get("quarantine_reason") or None,
                "content_hash": meta.get("content_hash", "")[:12], "token_count": meta.get("token_count"),
                "embedding_dim": len(embeddings[i]) if embeddings is not None else None,
                "embedding_preview": [round(float(v), 4) for v in embeddings[i][:5]] if embeddings is not None else None,
                "text_preview": doc[:160],
            } for i, (cid, doc, meta) in enumerate(zip(got["ids"], got["documents"], got["metadatas"], strict=True))],
        })
    return out
