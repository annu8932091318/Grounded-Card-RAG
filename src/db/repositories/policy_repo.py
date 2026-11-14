"""Policy corpus storage in ChromaDB (see src/db/chroma.py): one searchable collection per embedding model plus one
quarantine collection. Every read filters by tenant and only searchable collections are queried, so quarantined /
non-policy text can never reach the model.

Row shape returned to retrieval: {chunk_id, doc_id, section, text, text_norm, version, sim?}
chunk_id = "<tenant>:<doc_id>:<section>" (stable, so re-ingesting a doc replaces its rows in place; identical across
the per-model collections, so BM25 and vector results fuse on it).
"""
import hashlib

from src.db import chroma


def chunk_id(tenant: str, doc_id: str, section: str) -> str:
    return f"{tenant}:{doc_id}:{section}"


def _row(cid: str, document: str, meta: dict) -> dict:
    return {"chunk_id": cid, "doc_id": meta["doc_id"], "section": meta["section"], "text": document,
            "text_norm": meta.get("text_norm", ""), "version": meta.get("version")}


def _where(doc_id: str, tenant: str) -> dict:
    return {"$and": [{"doc_id": doc_id}, {"tenant_id": tenant}]}


def get_doc_hash(doc_id: str, tenant: str, model: str) -> str | None:
    """content_hash of this doc in this model's collection (or in quarantine), None if not there."""
    for col in (chroma.policy_collection(model), chroma.quarantine_collection()):
        got = col.get(where=_where(doc_id, tenant), limit=1, include=["metadatas"])
        if got["ids"]:
            return got["metadatas"][0]["content_hash"]
    return None


def _metadatas(doc: dict, chunks: list[dict], model: str) -> list[dict]:
    return [{
        "doc_id": doc["doc_id"], "tenant_id": doc["tenant_id"], "section": c["section"], "title": doc["title"],
        "version": doc["version"] or "", "owner": doc["owner"] or "", "effective_date": str(doc["effective_date"] or ""),
        "authority": doc["authority"], "quarantined": bool(doc["quarantined"]),
        "quarantine_reason": doc["quarantine_reason"] or "", "content_hash": doc["content_hash"],
        "text_norm": c["text_norm"], "token_count": c["token_count"], "embedding_model": model,
    } for c in chunks]


def replace_doc(doc: dict, chunks: list[dict], vectors: list[list[float]], model: str) -> None:
    """Replace this doc's chunks in `model`'s collection (or in quarantine). Chroma has no multi-collection
    transaction: delete-then-add is safe because ingest is single-writer and idempotent (re-run fixes a crash)."""
    searchable = chroma.policy_collection(model)
    quarantine = chroma.quarantine_collection()
    for col in (searchable, quarantine):  # a doc can move between them when its content changes
        col.delete(where=_where(doc["doc_id"], doc["tenant_id"]))
    target = quarantine if doc["quarantined"] else searchable
    if chunks:
        target.add(ids=[chunk_id(doc["tenant_id"], doc["doc_id"], c["section"]) for c in chunks],
                   documents=[c["text"] for c in chunks], embeddings=vectors, metadatas=_metadatas(doc, chunks, model))


def searchable_models(tenant: str = "default") -> list[str]:
    """Embedding models whose collection actually holds this tenant's corpus (query only those)."""
    from src.const.config import config

    out = []
    for model in config.embedding_models:
        if chroma.policy_collection(model).get(where={"tenant_id": tenant}, limit=1, include=[])["ids"]:
            out.append(model)
    return out


def vector_search(embedding: list[float], model: str, tenant: str, limit: int) -> list[dict]:
    col = chroma.policy_collection(model)
    n = col.count()
    if n == 0:
        return []
    res = col.query(query_embeddings=[embedding], n_results=min(limit, n),
                    where={"$and": [{"tenant_id": tenant}, {"authority": "policy"}]},
                    include=["documents", "metadatas", "distances"])
    return [{**_row(cid, doc, meta), "sim": 1.0 - float(dist)}  # cosine distance -> similarity
            for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0], strict=True)]


def _first_populated(tenant: str):
    models = searchable_models(tenant)
    return chroma.policy_collection(models[0]) if models else chroma.policy_collection()


def all_chunks(tenant: str) -> list[dict]:
    """Every searchable chunk for the tenant (the BM25 leg indexes these; the text is identical in every collection)."""
    got = _first_populated(tenant).get(where={"$and": [{"tenant_id": tenant}, {"authority": "policy"}]},
                                       include=["documents", "metadatas"])
    return [_row(cid, doc, meta) for cid, doc, meta in zip(got["ids"], got["documents"], got["metadatas"], strict=True)]


def get_chunk(doc_id: str, section: str, tenant: str) -> dict | None:
    got = _first_populated(tenant).get(ids=[chunk_id(tenant, doc_id, section)], include=["documents", "metadatas"])
    if not got["ids"] or got["metadatas"][0].get("authority") != "policy":
        return None
    return _row(got["ids"][0], got["documents"][0], got["metadatas"][0])


def stats(tenant: str = "default") -> dict:
    from src.const.config import config

    per_model = {m: len(chroma.policy_collection(m).get(where={"tenant_id": tenant}, include=[])["ids"])
                 for m in config.embedding_models}
    searchable = _first_populated(tenant).get(where={"tenant_id": tenant}, include=["metadatas"])
    quarantined = chroma.quarantine_collection().get(where={"tenant_id": tenant}, include=["metadatas"])
    return {
        "docs": len({m["doc_id"] for m in searchable["metadatas"]} | {m["doc_id"] for m in quarantined["metadatas"]}),
        "chunks": max(per_model.values() or [0]),
        "chunks_per_embedding_model": per_model,
        "quarantined": sorted({m["doc_id"] for m in quarantined["metadatas"]}),
    }


def corpus_hash(tenant: str = "default") -> str:
    """Changes whenever any doc changes (part of the semantic-cache scope)."""
    hashes = set()
    for col in (_first_populated(tenant), chroma.quarantine_collection()):
        got = col.get(where={"tenant_id": tenant}, include=["metadatas"])
        hashes |= {f"{m['doc_id']}={m['content_hash']}" for m in got["metadatas"]}
    return hashlib.md5(",".join(sorted(hashes)).encode()).hexdigest()


def remove_doc(doc_id: str, tenant: str, model: str) -> None:
    """Drop a doc from one model's searchable collection (e.g. it became quarantined)."""
    chroma.policy_collection(model).delete(where=_where(doc_id, tenant))
