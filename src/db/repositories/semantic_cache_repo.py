"""semantic_cache table access. The matching logic lives in src/modules/cache/semantic_cache.py."""
import json

from src.db.index import query, query_one


def get_exact(cache_key: str) -> dict | None:
    return query_one("SELECT * FROM semantic_cache WHERE cache_key = %s AND expires_at > now()", (cache_key,))


def candidates(tenant: str, prompt_version: str, corpus_hash: str, limit: int) -> list[dict]:
    """Live entries in scope, newest first. The cosine comparison runs in Python (numpy): there is no vector index
    in Postgres here (no pgvector). Fine for thousands of entries; beyond that -> Redis vector index / pgvector."""
    return query(
        """SELECT cache_key, question_norm, embedding, answer, cited_sections, model_used FROM semantic_cache
           WHERE tenant_id = %s AND prompt_version = %s AND corpus_hash = %s AND expires_at > now()
           ORDER BY created_at DESC LIMIT %s""",
        (tenant, prompt_version, corpus_hash, limit),
    )


def put(entry: dict) -> None:
    query(
        """INSERT INTO semantic_cache (cache_key, tenant_id, prompt_version, corpus_hash, question_norm, embedding,
                                       answer, cited_sections, model_used, expires_at)
           VALUES (%(cache_key)s, %(tenant_id)s, %(prompt_version)s, %(corpus_hash)s, %(question_norm)s, %(embedding)s,
                   %(answer)s::jsonb, %(cited_sections)s, %(model_used)s, now() + make_interval(secs => %(ttl_s)s))
           ON CONFLICT (cache_key) DO UPDATE SET answer = EXCLUDED.answer, embedding = EXCLUDED.embedding,
             cited_sections = EXCLUDED.cited_sections, model_used = EXCLUDED.model_used,
             created_at = now(), expires_at = EXCLUDED.expires_at""",
        {**entry, "answer": json.dumps(entry["answer"], default=str)},
    )


def record_hit(cache_key: str) -> None:
    query("UPDATE semantic_cache SET hit_count = hit_count + 1, last_hit_at = now() WHERE cache_key = %s", (cache_key,))


def delete_stale(tenant: str, corpus_hash: str) -> int:
    """After ingest: answers built on an older corpus are no longer trustworthy."""
    rows = query("DELETE FROM semantic_cache WHERE tenant_id = %s AND (corpus_hash <> %s OR expires_at < now()) "
                 "RETURNING cache_key", (tenant, corpus_hash))
    return len(rows)


def clear(tenant: str | None = None) -> int:
    rows = query("DELETE FROM semantic_cache WHERE (%s::text IS NULL OR tenant_id = %s) RETURNING cache_key", (tenant, tenant))
    return len(rows)


def stats() -> dict:
    row = query_one("""SELECT count(*) AS entries, coalesce(sum(hit_count), 0) AS total_hits,
                              count(*) FILTER (WHERE expires_at < now()) AS expired,
                              max(created_at) AS newest, max(last_hit_at) AS last_hit
                       FROM semantic_cache""")
    top = query("""SELECT question_norm, hit_count, model_used, created_at, expires_at FROM semantic_cache
                   ORDER BY hit_count DESC, created_at DESC LIMIT 10""")
    return {**(row or {}), "top_entries": top}
