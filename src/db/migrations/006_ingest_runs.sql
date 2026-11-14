-- Audit trail of corpus ingests into ChromaDB (the vectors live in Chroma; the history lives here).
CREATE TABLE IF NOT EXISTS ingest_runs (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         text NOT NULL,
  docs              int,
  chunks            int,
  ingested          text[],
  skipped_unchanged text[],
  quarantined       text[],
  chroma_path       text,
  duration_ms       int,
  created_at        timestamptz NOT NULL DEFAULT now()
);
