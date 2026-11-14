"""Policy corpus storage in ChromaDB (see src/db/chroma.py). Every read filters by tenant, and only the
searchable collection is ever queried, so quarantined / non-policy text can never reach the model.

Row shape returned to retrieval: {chunk_id, doc_id, section, text, text_norm, version, sim?}
chunk_id = "<tenant>:<doc_id>:<section>" (stable, so re-ingesting a doc replaces its rows in place).
"""
import hashlib

from src.db import chroma


def chunk_id(tenant: str, doc_id: str, section: str) -> str:
    return f"{tenant}:{doc_id}:{section}"


def _row(cid: str, document: str, meta: dict) -> dict:
    return {"chunk_id": cid, "doc_id": meta["doc_id"], "section": meta["section"], "text": document,
            "text_norm": meta.get("text_norm", ""), "version": meta.get("version")}


def get_doc_hash(doc_id: str, tenant: str = "default") -> str | None:
    for col in (chroma.policy_collection(), chroma.quarantine_collection()):
        got = col.get(where={"$and": [{"doc_id": doc_id}, {"tenant_id": tenant}]}, limit=1, include=["metadatas"])
        if got["ids"]:
            return got["metadatas"][0]["content_hash"]
    return None


def replace_doc(doc: dict, chunks: list[dict]) -> None:
    """Delete every chunk of this doc (from both collections) and write the new ones to the right collection.
    Chroma has no multi-collection transaction: delete-then-add is safe here because ingest is single-writer
    and idempotent (a crash mid-way is fixed by re-running ingest; content_hash makes the re-run cheap)."""
    where = {"$and": [{"doc_id": doc["doc_id"]}, {"tenant_id": doc["tenant_id"]}]}
    for col in (chroma.policy_collection(), chroma.quarantine_collection()):
        col.delete(where=where)
    target = chroma.quarantine_collection() if doc["quarantined"] else chroma.policy_collection()
    if not chunks:
        return
    target.add(
        ids=[chunk_id(doc["tenant_id"], doc["doc_id"], c["section"]) for c in chunks],
        documents=[c["text"] for c in chunks],
        embeddings=[c["embedding"] for c in chunks],
        metadatas=[{
            "doc_id": doc["doc_id"], "tenant_id": doc["tenant_id"], "section": c["section"],
            "title": doc["title"], "version": doc["version"] or "", "owner": doc["owner"] or "",
            "effective_date": str(doc["effective_date"] or ""), "authority": doc["authority"],
            "quarantined": bool(doc["quarantined"]), "quarantine_reason": doc["quarantine_reason"] or "",
            "content_hash": doc["content_hash"], "text_norm": c["text_norm"], "token_count": c["token_count"],
        } for c in chunks],
    )


def vector_search(embedding: list[float], tenant: str, limit: int) -> list[dict]:
    col = chroma.policy_collection()
    n = col.count()
    if n == 0:
        return []
    res = col.query(query_embeddings=[embedding], n_results=min(limit, n),
                    where={"$and": [{"tenant_id": tenant}, {"authority": "policy"}]},
                    include=["documents", "metadatas", "distances"])
    rows = []
    for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0], strict=True):
        rows.append({**_row(cid, doc, meta), "sim": 1.0 - float(dist)})  # cosine distance -> similarity
    return rows


def all_chunks(tenant: str) -> list[dict]:
    """Every searchable chunk for the tenant (the keyword/BM25 leg indexes these in memory)."""
    got = chroma.policy_collection().get(where={"$and": [{"tenant_id": tenant}, {"authority": "policy"}]},
                                         include=["documents", "metadatas"])
    return [_row(cid, doc, meta) for cid, doc, meta in zip(got["ids"], got["documents"], got["metadatas"], strict=True)]


def get_chunk(doc_id: str, section: str, tenant: str) -> dict | None:
    got = chroma.policy_collection().get(ids=[chunk_id(tenant, doc_id, section)], include=["documents", "metadatas"])
    if not got["ids"] or got["metadatas"][0].get("authority") != "policy":
        return None
    return _row(got["ids"][0], got["documents"][0], got["metadatas"][0])


def stats(tenant: str = "default") -> dict:
    searchable = chroma.policy_collection().get(where={"tenant_id": tenant}, include=["metadatas"])
    quarantined = chroma.quarantine_collection().get(where={"tenant_id": tenant}, include=["metadatas"])
    return {
        "docs": len({m["doc_id"] for m in searchable["metadatas"]} | {m["doc_id"] for m in quarantined["metadatas"]}),
        "chunks": len(searchable["ids"]),
        "quarantined": sorted({m["doc_id"] for m in quarantined["metadatas"]}),
    }


def corpus_hash(tenant: str = "default") -> str:
    """Changes whenever any doc changes."""
    hashes = set()
    for col in (chroma.policy_collection(), chroma.quarantine_collection()):
        got = col.get(where={"tenant_id": tenant}, include=["metadatas"])
        hashes |= {f"{m['doc_id']}={m['content_hash']}" for m in got["metadatas"]}
    return hashlib.md5(",".join(sorted(hashes)).encode()).hexdigest()
