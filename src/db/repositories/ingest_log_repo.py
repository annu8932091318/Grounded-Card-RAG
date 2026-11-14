"""ingest_log: one row per document per embedding model per ingest run."""
from src.db.index import query


def save(rows: list[dict]) -> None:
    for r in rows:
        query(
            """INSERT INTO ingest_log (run_id, tenant_id, doc_id, file_name, embedding_model, action, chunks, content_hash,
                                       version, reason, duration_ms, chroma_path)
               VALUES (%(run_id)s, %(tenant_id)s, %(doc_id)s, %(file_name)s, %(embedding_model)s, %(action)s, %(chunks)s,
                       %(content_hash)s, %(version)s, %(reason)s, %(duration_ms)s, %(chroma_path)s)""",
            r,
        )


def list_runs(limit: int = 20) -> list[dict]:
    """One line per run: when, how many docs ingested / skipped / quarantined / failed."""
    return query(
        """SELECT run_id, tenant_id, min(created_at) AS started_at, count(DISTINCT doc_id) AS docs,
                  array_agg(DISTINCT embedding_model) AS embedding_models,
                  sum((action = 'ingested')::int) AS ingested, sum((action = 'skipped_unchanged')::int) AS skipped,
                  sum((action = 'quarantined')::int) AS quarantined, sum((action = 'failed')::int) AS failed,
                  sum(chunks) AS chunks_written, sum(duration_ms) AS duration_ms
           FROM ingest_log GROUP BY run_id, tenant_id ORDER BY started_at DESC LIMIT %s""",
        (limit,),
    )


def get_run(run_id: str) -> list[dict]:
    return query("SELECT * FROM ingest_log WHERE run_id::text = %s ORDER BY id", (run_id,))
