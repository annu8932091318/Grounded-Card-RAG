"""policy_docs / policy_chunks. Every read filters by tenant AND excludes non-policy / quarantined docs."""
from src.db.index import query, query_one, to_vector, transaction

# Shared WHERE clause: this is where quarantine is enforced for BOTH retrieval legs.
_ALLOWED = "c.tenant_id = %(tenant)s AND d.authority = 'policy' AND NOT d.quarantined"


def get_doc_hash(doc_id: str) -> str | None:
    row = query_one("SELECT content_hash FROM policy_docs WHERE doc_id = %s", (doc_id,))
    return row["content_hash"] if row else None


def replace_doc(doc: dict, chunks: list[dict]) -> None:
    """Upsert one doc and replace all its chunks atomically."""
    with transaction() as conn:
        conn.execute(
            """INSERT INTO policy_docs (doc_id, tenant_id, title, version, effective_date, owner,
                                        authority, quarantined, quarantine_reason, content_hash, ingested_at)
               VALUES (%(doc_id)s, %(tenant_id)s, %(title)s, %(version)s, %(effective_date)s, %(owner)s,
                       %(authority)s, %(quarantined)s, %(quarantine_reason)s, %(content_hash)s, now())
               ON CONFLICT (doc_id) DO UPDATE SET
                 tenant_id = EXCLUDED.tenant_id, title = EXCLUDED.title, version = EXCLUDED.version,
                 effective_date = EXCLUDED.effective_date, owner = EXCLUDED.owner,
                 authority = EXCLUDED.authority, quarantined = EXCLUDED.quarantined,
                 quarantine_reason = EXCLUDED.quarantine_reason, content_hash = EXCLUDED.content_hash,
                 ingested_at = now()""",
            doc,
        )
        conn.execute("DELETE FROM policy_chunks WHERE doc_id = %s", (doc["doc_id"],))
        for ch in chunks:
            conn.execute(
                """INSERT INTO policy_chunks (doc_id, tenant_id, section, text, text_norm, token_count, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s, %s::vector)""",
                (doc["doc_id"], doc["tenant_id"], ch["section"], ch["text"], ch["text_norm"],
                 ch["token_count"], to_vector(ch["embedding"])),
            )


def keyword_search(tsquery: str, tenant: str, limit: int) -> list[dict]:
    if not tsquery:
        return []
    return query(
        f"""SELECT c.chunk_id, c.doc_id, c.section, c.text, c.text_norm, d.version, ts_rank_cd(c.tsv, q) AS kw
            FROM policy_chunks c
            JOIN policy_docs d ON d.doc_id = c.doc_id AND d.tenant_id = c.tenant_id,
                 to_tsquery('simple', %(q)s) q
            WHERE {_ALLOWED} AND c.tsv @@ q
            ORDER BY kw DESC LIMIT %(limit)s""",
        {"q": tsquery, "tenant": tenant, "limit": limit},
    )


def vector_search(embedding, tenant: str, limit: int) -> list[dict]:
    return query(
        f"""SELECT c.chunk_id, c.doc_id, c.section, c.text, d.version,
                   1 - (c.embedding <=> %(vec)s::vector) AS sim
            FROM policy_chunks c
            JOIN policy_docs d ON d.doc_id = c.doc_id AND d.tenant_id = c.tenant_id
            WHERE {_ALLOWED}
            ORDER BY c.embedding <=> %(vec)s::vector LIMIT %(limit)s""",
        {"vec": to_vector(embedding), "tenant": tenant, "limit": limit},
    )


def get_chunk(doc_id: str, section: str, tenant: str) -> dict | None:
    return query_one(
        f"""SELECT c.chunk_id, c.doc_id, c.section, c.text, d.version
            FROM policy_chunks c JOIN policy_docs d ON d.doc_id = c.doc_id AND d.tenant_id = c.tenant_id
            WHERE {_ALLOWED} AND c.doc_id = %(doc)s AND c.section = %(section)s""",
        {"tenant": tenant, "doc": doc_id, "section": section},
    )


def stats(tenant: str = "default") -> dict:
    docs = query("SELECT doc_id, quarantined FROM policy_docs WHERE tenant_id = %s", (tenant,))
    chunks = query_one("SELECT count(*) AS n FROM policy_chunks WHERE tenant_id = %s", (tenant,))
    return {
        "docs": len(docs),
        "chunks": chunks["n"] if chunks else 0,
        "quarantined": sorted(d["doc_id"] for d in docs if d["quarantined"]),
    }


def corpus_hash(tenant: str = "default") -> str:
    """Changes whenever any doc changes — part of retrieval/answer cache keys."""
    row = query_one(
        "SELECT md5(coalesce(string_agg(content_hash, ',' ORDER BY doc_id), '')) AS h FROM policy_docs WHERE tenant_id = %s",
        (tenant,),
    )
    return row["h"] if row else ""
