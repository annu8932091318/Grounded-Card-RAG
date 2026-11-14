"""Chunk -> embed -> upsert policy_docs/policy_chunks, with the authority + quarantine scan (defect D2).
Idempotent: a doc whose content_hash is unchanged is skipped. Also used by POST /admin/ingest.
Usage: python -m scripts.ingest"""
import json

from src.const.config import config
from src.db.repositories import policy_repo
from src.helper.hash import sha256
from src.modules.cache import lru
from src.modules.guardrails.input_guard import scan_corpus_doc
from src.modules.retrieval.chunker import chunk_doc, parse_doc
from src.modules.retrieval.embedder import embed_documents
from src.modules.telemetry.logger import log


def run_ingest(tenant_id: str = "default") -> dict:
    corpus_dir = config.data_dir / "policy_corpus"
    skipped = []
    for path in sorted(corpus_dir.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        doc = parse_doc(raw)
        content_hash = sha256(raw)
        if policy_repo.get_doc_hash(doc["doc_id"]) == content_hash:
            skipped.append(doc["doc_id"])
            continue

        reason = scan_corpus_doc(raw)
        if reason:
            # Stored for audit, never retrievable, never citable.
            log("corpus_injection_detected" if "injection" in reason else "corpus_non_policy_detected",
                level="warning", doc_id=doc["doc_id"], reason=reason)

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
        log("doc_ingested", doc_id=doc["doc_id"], chunks=len(chunks), quarantined=bool(reason))

    lru.clear("retrieval")  # corpus changed -> cached retrievals and answers are stale
    lru.clear("answer")
    return {**policy_repo.stats(tenant_id), "skipped_unchanged": skipped}


if __name__ == "__main__":
    print(json.dumps(run_ingest(), indent=2))
