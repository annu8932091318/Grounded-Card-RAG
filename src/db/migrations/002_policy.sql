CREATE TABLE IF NOT EXISTS policy_docs (
  doc_id            text PRIMARY KEY,                      -- 'POL-CRD-001'
  tenant_id         text NOT NULL DEFAULT 'default',
  title             text NOT NULL,
  version           text,
  effective_date    date,
  owner             text,
  authority         text NOT NULL CHECK (authority IN ('policy','non_policy')),
  quarantined       boolean NOT NULL DEFAULT false,        -- true for POL-OPS-012 (injection / draft)
  quarantine_reason text,
  content_hash      text NOT NULL,
  ingested_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS policy_chunks (
  chunk_id    bigserial PRIMARY KEY,
  doc_id      text NOT NULL REFERENCES policy_docs(doc_id) ON DELETE CASCADE,
  tenant_id   text NOT NULL DEFAULT 'default',
  section     text,                                        -- '3', 'Classic Card', 'Table', 'body'
  text        text NOT NULL,                               -- original text: shown, quoted, cited
  text_norm   text NOT NULL,                               -- normalised numbers: matched by FTS
  token_count int,
  tsv         tsvector GENERATED ALWAYS AS (to_tsvector('simple', text_norm || ' ' || doc_id)) STORED,
  embedding   vector(384) NOT NULL
);
CREATE INDEX IF NOT EXISTS policy_chunks_tsv_idx    ON policy_chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS policy_chunks_vec_idx    ON policy_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS policy_chunks_tenant_idx ON policy_chunks (tenant_id, doc_id);
