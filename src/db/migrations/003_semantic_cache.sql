-- 3. semantic_cache: answers reused WITHOUT calling the LLM.
--    Exact hit: cache_key = sha256(tenant | prompt_version | corpus_hash | normalised question).
--    Semantic hit: cosine(question embedding, stored embedding) >= SEMANTIC_CACHE_MIN_SIM (0.95), same numbers in the
--    question, cited sections still retrieved, output guard passes for the asking customer.
--    Only account-independent answers are stored (no account data, no service request, no flags, no history).
--    Scale-out path: Redis (RediSearch vector index / Redis LangCache) or pgvector — see README.
CREATE TABLE IF NOT EXISTS semantic_cache (
  cache_key      text PRIMARY KEY,
  tenant_id      text NOT NULL,
  prompt_version text NOT NULL,
  corpus_hash    text NOT NULL,                     -- the corpus changed -> entry is stale (ingest deletes them)
  question_norm  text NOT NULL,                     -- PII-masked, normalised question
  embedding      real[] NOT NULL,                   -- 384-d all-MiniLM-L6-v2, L2-normalised
  answer         jsonb NOT NULL,                    -- the validated respond payload
  cited_sections text[] NOT NULL DEFAULT '{}',      -- 'POL-FEE-007|Table' — re-checked against fresh retrieval on a hit
  model_used     text,
  hit_count      int NOT NULL DEFAULT 0,
  created_at     timestamptz NOT NULL DEFAULT now(),
  last_hit_at    timestamptz,
  expires_at     timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS semantic_cache_scope_idx ON semantic_cache (tenant_id, prompt_version, corpus_hash, expires_at);
