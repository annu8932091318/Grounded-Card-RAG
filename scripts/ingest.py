"""Ingest pipeline: read .md -> parse header -> quarantine scan -> chunk by section -> embed (local MiniLM)
-> write to ChromaDB (searchable or quarantine collection) -> one ingest_log row per document in Postgres.

Idempotent: a doc whose content_hash is unchanged is skipped. A changed corpus also deletes semantic_cache
entries built on the old corpus. Also used by POST /api/v1/admin/ingest.
Usage:  python -m scripts.ingest            (add --force to re-embed everything)
"""
import argparse
import json
import time
import uuid

from src.const.config import config
from src.db.repositories import policy_repo
from src.helper.hash import sha256
from src.modules.cache import lru, semantic_cache
from src.modules.guardrails.input_guard import scan_corpus_doc
from src.modules.retrieval import keyword_search
from src.modules.retrieval.chunker import chunk_doc, parse_doc
from src.modules.retrieval.embedder import embed_documents
from src.modules.telemetry.logger import log


def run_ingest(tenant_id: str = "default", force: bool = False) -> dict:
    t0 = time.perf_counter()
    run_id = str(uuid.uuid4())
    corpus_dir = config.data_dir / "policy_corpus"
    rows: list[dict] = []
    log("ingest_started", run_id=run_id, corpus_dir=str(corpus_dir), tenant=tenant_id, force=force)
    for path in sorted(corpus_dir.glob("*.md")):
        d0 = time.perf_counter()
        row = {"run_id": run_id, "tenant_id": tenant_id, "doc_id": path.stem.split("_")[0], "file_name": path.name,
               "action": "failed", "chunks": 0, "content_hash": None, "version": None, "reason": None,
               "chroma_path": str(config.chroma_path)}
        try:
            raw = path.read_text(encoding="utf-8")
            doc = parse_doc(raw)
            row.update(doc_id=doc["doc_id"], version=doc["version"], content_hash=sha256(raw))
            if not force and policy_repo.get_doc_hash(doc["doc_id"], tenant_id) == row["content_hash"]:
                row["action"] = "skipped_unchanged"
            else:
                reason = scan_corpus_doc(raw)
                if reason:
                    # Stored for audit in the quarantine collection: never retrievable, never citable.
                    log("corpus_doc_quarantined", level="warning", doc_id=doc["doc_id"], reason=reason)
                chunks = chunk_doc(doc)
                vectors = embed_documents([c["text_norm"] for c in chunks])
                for c, v in zip(chunks, vectors, strict=True):
                    c["embedding"] = v
                policy_repo.replace_doc({
                    "doc_id": doc["doc_id"], "tenant_id": tenant_id, "title": doc["title"], "version": doc["version"],
                    "effective_date": doc["effective_date"], "owner": doc["owner"],
                    "authority": "non_policy" if reason else "policy", "quarantined": bool(reason),
                    "quarantine_reason": reason, "content_hash": row["content_hash"],
                }, chunks)
                row.update(action="quarantined" if reason else "ingested", chunks=len(chunks), reason=reason)
                log("doc_ingested", doc_id=doc["doc_id"], chunks=len(chunks), dim=len(vectors[0]) if vectors else 0,
                    quarantined=bool(reason))
        except Exception as e:  # one bad file must not stop the others; it is recorded as failed
            row["reason"] = f"{type(e).__name__}: {str(e)[:300]}"
            log("doc_ingest_failed", level="error", exc_info=True, file=path.name)
        row["duration_ms"] = int((time.perf_counter() - d0) * 1000)
        rows.append(row)

    changed = any(r["action"] in ("ingested", "quarantined") for r in rows)
    if changed:
        keyword_search.clear_index()  # corpus changed -> BM25 index, cached retrievals and cached answers are stale
        lru.clear("retrieval")
    stale_answers = semantic_cache.invalidate(tenant_id) if changed else 0
    _record(rows)
    result = {**policy_repo.stats(tenant_id), "run_id": run_id,
              "ingested": [r["doc_id"] for r in rows if r["action"] == "ingested"],
              "skipped_unchanged": [r["doc_id"] for r in rows if r["action"] == "skipped_unchanged"],
              "quarantined_this_run": [r["doc_id"] for r in rows if r["action"] == "quarantined"],
              "failed": [{"file": r["file_name"], "reason": r["reason"]} for r in rows if r["action"] == "failed"],
              "stale_cached_answers_deleted": stale_answers,
              "chroma_path": str(config.chroma_path), "duration_ms": int((time.perf_counter() - t0) * 1000)}
    log("ingest_completed", run_id=run_id, docs=result["docs"], chunks=result["chunks"], ingested=len(result["ingested"]),
        skipped=len(result["skipped_unchanged"]), failed=len(result["failed"]), duration_ms=result["duration_ms"])
    return result


def _record(rows: list[dict]) -> None:
    """Postgres ingest_log keeps the audit trail. Best effort: ChromaDB is the source of truth for vectors."""
    try:
        from src.db.repositories import ingest_log_repo

        ingest_log_repo.save(rows)
    except Exception as e:
        log("ingest_log_not_recorded", level="warning", error=type(e).__name__, detail=str(e)[:160])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-embed every document even if unchanged")
    ap.add_argument("--tenant", default="default")
    args = ap.parse_args()
    print(json.dumps(run_ingest(args.tenant, args.force), indent=2))
