-- 2. ingest_log: ONE row per document per ingest run (what was embedded into ChromaDB, skipped or quarantined).
CREATE TABLE IF NOT EXISTS ingest_log (
  id            bigserial PRIMARY KEY,
  run_id        uuid NOT NULL,                      -- all rows of one `python -m scripts.ingest` / POST /admin/ingest
  tenant_id     text NOT NULL,
  doc_id        text NOT NULL,
  file_name     text,
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
