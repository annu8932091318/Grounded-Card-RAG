"""Ingest pipeline: read .md -> parse header -> quarantine scan -> chunk by section -> embed with EVERY model in the
embedding chain (Gemini API x2 + local bge-small) -> one ChromaDB collection per model -> ingest_log rows in Postgres.

One ingest_log row per document per model: a model that fails (quota, network) is recorded as 'failed' for that
model only; the others still get the document, and the next run retries just the missing one (content-hash skip).
Quarantined documents are embedded once, locally (never searched, so never worth API quota).
A changed corpus also deletes semantic_cache entries built on the old corpus.
Usage:  python -m scripts.ingest            (add --force to re-embed everything)
Also used by POST /api/v1/admin/ingest.
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
from src.modules.retrieval import embedder, keyword_search
from src.modules.retrieval.chunker import chunk_doc, parse_doc
from src.modules.telemetry.logger import log


def _row(run_id, tenant_id, path, model) -> dict:
    return {"run_id": run_id, "tenant_id": tenant_id, "doc_id": path.stem.split("_")[0], "file_name": path.name,
            "embedding_model": model, "action": "failed", "chunks": 0, "content_hash": None, "version": None,
            "reason": None, "duration_ms": 0, "chroma_path": str(config.chroma_path)}


def _ingest_with(model: str, doc: dict, chunks: list[dict], meta: dict, row: dict, force: bool) -> None:
    t0 = time.perf_counter()
    try:
        if not force and policy_repo.get_doc_hash(doc["doc_id"], meta["tenant_id"], model) == meta["content_hash"]:
            row["action"] = "skipped_unchanged"
            return
        vectors = embedder.embed_documents([c["text_norm"] for c in chunks], model,
                                           titles=[f"{doc['doc_id']} {doc['title']}"] * len(chunks))
        policy_repo.replace_doc(meta, chunks, vectors, model)
        row.update(action="quarantined" if meta["quarantined"] else "ingested", chunks=len(chunks),
                   reason=meta["quarantine_reason"])
        log("doc_ingested", doc_id=doc["doc_id"], model=model, chunks=len(chunks), dim=len(vectors[0]) if vectors else 0,
            quarantined=meta["quarantined"])
    except Exception as e:  # this model failed for this doc; the other models still get it
        row["reason"] = f"{type(e).__name__}: {str(e)[:300]}"
        log("doc_ingest_failed", level="error", doc_id=doc["doc_id"], model=model, error=type(e).__name__,
            detail=str(e)[:200])
    finally:
        row["duration_ms"] = int((time.perf_counter() - t0) * 1000)


def run_ingest(tenant_id: str = "default", force: bool = False) -> dict:
    t0 = time.perf_counter()
    run_id = str(uuid.uuid4())
    corpus_dir = config.data_dir / "policy_corpus"
    rows: list[dict] = []
    log("ingest_started", run_id=run_id, corpus_dir=str(corpus_dir), tenant=tenant_id, force=force,
        models=embedder.models())
    from src.db import chroma

    dropped = chroma.prune_collections()  # models removed from the chain leave no orphan collection behind
    if dropped:
        log("chroma_collections_dropped", level="warning", collections=dropped)
    for path in sorted(corpus_dir.glob("*.md")):
        try:
            raw = path.read_text(encoding="utf-8")
            doc = parse_doc(raw)
        except Exception as e:  # unreadable file: one failed row, carry on
            row = _row(run_id, tenant_id, path, None)
            row["reason"] = f"{type(e).__name__}: {str(e)[:300]}"
            rows.append(row)
            log("doc_parse_failed", level="error", file=path.name, error=type(e).__name__)
            continue
        reason = scan_corpus_doc(raw)
        meta = {"doc_id": doc["doc_id"], "tenant_id": tenant_id, "title": doc["title"], "version": doc["version"],
                "effective_date": doc["effective_date"], "owner": doc["owner"],
                "authority": "non_policy" if reason else "policy", "quarantined": bool(reason),
                "quarantine_reason": reason, "content_hash": sha256(raw)}
        chunks = chunk_doc(doc)
        if reason:
            # Stored for audit in the quarantine collection: never retrievable, never citable. Embedded locally once.
            log("corpus_doc_quarantined", level="warning", doc_id=doc["doc_id"], reason=reason)
            for model in embedder.models():
                policy_repo.remove_doc(doc["doc_id"], tenant_id, model)
            targets = [embedder.local_fallback()]
        else:
            targets = embedder.models()
        for model in targets:
            row = {**_row(run_id, tenant_id, path, model), "doc_id": doc["doc_id"], "version": doc["version"],
                   "content_hash": meta["content_hash"]}
            _ingest_with(model, doc, chunks, meta, row, force)
            rows.append(row)

    changed = bool(dropped) or any(r["action"] in ("ingested", "quarantined") for r in rows)
    if changed:
        keyword_search.clear_index()  # corpus changed -> BM25 index, cached retrievals and cached answers are stale
        lru.clear("retrieval")
    stale_answers = semantic_cache.invalidate(tenant_id) if changed else 0
    _record(rows)
    by_model = {m: {a: sum(1 for r in rows if r["embedding_model"] == m and r["action"] == a)
                    for a in ("ingested", "skipped_unchanged", "quarantined", "failed")}
                for m in {r["embedding_model"] for r in rows if r["embedding_model"]}}
    result = {**policy_repo.stats(tenant_id), "run_id": run_id, "by_embedding_model": by_model,
              "failed": [{"doc_id": r["doc_id"], "model": r["embedding_model"], "reason": r["reason"]}
                         for r in rows if r["action"] == "failed"],
              "stale_cached_answers_deleted": stale_answers, "collections_dropped": dropped,
              "chroma_path": str(config.chroma_path), "duration_ms": int((time.perf_counter() - t0) * 1000)}
    log("ingest_completed", run_id=run_id, docs=result["docs"], by_model=by_model, failed=len(result["failed"]),
        duration_ms=result["duration_ms"])
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
    print(json.dumps(run_ingest(args.tenant, args.force), indent=2, default=str))
