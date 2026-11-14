"""ingest_runs — audit trail of corpus ingests."""
from src.db.index import query


def save_run(tenant_id: str, r: dict) -> None:
    query(
        """INSERT INTO ingest_runs (tenant_id, docs, chunks, ingested, skipped_unchanged, quarantined, chroma_path, duration_ms)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (tenant_id, r["docs"], r["chunks"], r["ingested"], r["skipped_unchanged"], r["quarantined"],
         r["chroma_path"], r["duration_ms"]),
    )


def list_runs(limit: int = 20) -> list[dict]:
    return query("SELECT * FROM ingest_runs ORDER BY created_at DESC LIMIT %s", (limit,))
