-- 2. ingest_log: ONE row per document per embedding model per ingest run: what was embedded into which ChromaDB
--    collection (gemini-embedding-2 / gemini-embedding-001 / local bge-small), skipped, quarantined or failed.
CREATE TABLE IF NOT EXISTS ingest_log (
  id            bigserial PRIMARY KEY,
  run_id        uuid NOT NULL,                      -- all rows of one `python -m scripts.ingest` / POST /admin/ingest
  tenant_id     text NOT NULL,
  doc_id        text NOT NULL,
  file_name     text,
  embedding_model text,                             -- which model / Chroma collection this row is about
  action        text NOT NULL CHECK (action IN ('ingested','skipped_unchanged','quarantined','failed')),
  chunks        int NOT NULL DEFAULT 0,
  content_hash  text,
  version       text,
  reason        text,                               -- quarantine reason / error message
  duration_ms   int,
  chroma_path   text,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ingest_log_run_idx ON ingest_log (run_id);
CREATE INDEX IF NOT EXISTS ingest_log_doc_idx ON ingest_log (doc_id, created_at DESC);
ALTER TABLE ingest_log ADD COLUMN IF NOT EXISTS embedding_model text;
