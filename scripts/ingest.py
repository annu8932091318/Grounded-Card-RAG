"""Ingest pipeline: read .md -> parse header -> quarantine scan -> chunk by section -> embed (local MiniLM)
-> write to ChromaDB (searchable or quarantine collection) -> record the run in Postgres (ingest_runs).

Idempotent: a doc whose content_hash is unchanged is skipped. Also used by POST /api/v1/admin/ingest.
Usage:  python -m scripts.ingest            (add --force to re-embed everything)
"""
import argparse
import json
import time

from src.const.config import config
from src.db.repositories import policy_repo
from src.helper.hash import sha256
from src.modules.cache import lru
from src.modules.guardrails.input_guard import scan_corpus_doc
from src.modules.retrieval import keyword_search
from src.modules.retrieval.chunker import chunk_doc, parse_doc
from src.modules.retrieval.embedder import embed_documents
from src.modules.telemetry.logger import log


def run_ingest(tenant_id: str = "default", force: bool = False) -> dict:
    t0 = time.perf_counter()
    corpus_dir = config.data_dir / "policy_corpus"
    skipped, ingested, quarantined = [], [], []
    log("ingest_started", corpus_dir=str(corpus_dir), tenant=tenant_id, force=force)
    for path in sorted(corpus_dir.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        doc = parse_doc(raw)
        content_hash = sha256(raw)
        if not force and policy_repo.get_doc_hash(doc["doc_id"], tenant_id) == content_hash:
            skipped.append(doc["doc_id"])
            continue

        reason = scan_corpus_doc(raw)
        if reason:
            # Stored for audit in the quarantine collection: never retrievable, never citable.
            log("corpus_doc_quarantined", level="warning", doc_id=doc["doc_id"], reason=reason)
            quarantined.append(doc["doc_id"])

        chunks = chunk_doc(doc)
        vectors = embed_documents([c["text_norm"] for c in chunks])
        for c, v in zip(chunks, vectors, strict=True):
            c["embedding"] = v
        policy_repo.replace_doc({
            "doc_id": doc["doc_id"], "tenant_id": tenant_id, "title": doc["title"], "version": doc["version"],
            "effective_date": doc["effective_date"], "owner": doc["owner"],
            "authority": "non_policy" if reason else "policy", "quarantined": bool(reason),
            "quarantine_reason": reason, "content_hash": content_hash,
        }, chunks)
        ingested.append(doc["doc_id"])
        log("doc_ingested", doc_id=doc["doc_id"], chunks=len(chunks), dim=len(vectors[0]) if vectors else 0,
            quarantined=bool(reason))

    keyword_search.clear_index()  # corpus changed -> BM25 index, cached retrievals and answers are stale
    lru.clear("retrieval")
    lru.clear("answer")
    result = {**policy_repo.stats(tenant_id), "ingested": ingested, "skipped_unchanged": skipped,
              "quarantined_this_run": quarantined, "chroma_path": str(config.chroma_path),
              "duration_ms": int((time.perf_counter() - t0) * 1000)}
    _record_run(tenant_id, result)
    log("ingest_completed", docs=result["docs"], chunks=result["chunks"], ingested=len(ingested),
        skipped=len(skipped), duration_ms=result["duration_ms"])
    return result


def _record_run(tenant_id: str, result: dict) -> None:
    """Postgres keeps the audit trail of ingests (what changed, when). Best effort: Chroma is the source of truth."""
    try:
        from src.db.repositories import ingest_repo

        ingest_repo.save_run(tenant_id, result)
    except Exception as e:
        log("ingest_run_not_recorded", level="warning", error=type(e).__name__, detail=str(e)[:160])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-embed every document even if unchanged")
    ap.add_argument("--tenant", default="default")
    args = ap.parse_args()
    print(json.dumps(run_ingest(args.tenant, args.force), indent=2))
