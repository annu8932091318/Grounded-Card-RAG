CREATE TABLE IF NOT EXISTS traces (
  trace_id           uuid PRIMARY KEY,
  request_id         text,                                 -- = Idempotency-Key / x-request-id; joins request_logs + log file
  conversation_id    uuid,
  tenant_id          text,
  customer_id_hash   text,
  started_at         timestamptz NOT NULL,
  latency_ms         int,
  status             text,
  prompt_version     text,
  prompt_sha         text,
  provider           text,                                 -- 'gemini' | 'grok' | NULL (static mode)
  model_used         text,
  fallback_used      boolean DEFAULT false,
  llm_calls          int DEFAULT 0,
  input_tokens       int DEFAULT 0,
  output_tokens      int DEFAULT 0,
  cache_read_tokens  int DEFAULT 0,
  cost_usd           numeric(10,6) DEFAULT 0,
  cache_hit          boolean DEFAULT false,
  validation_retries int DEFAULT 0,
  error_code         text,
  flags              text[] DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS traces_started_idx ON traces(started_at DESC);

CREATE TABLE IF NOT EXISTS spans (
  span_id     uuid PRIMARY KEY,
  trace_id    uuid REFERENCES traces(trace_id) ON DELETE CASCADE,
  parent_id   uuid,
  name        text NOT NULL,                               -- 'llm.call','retrieval.hybrid','tool.get_account_state',...
  started_at  timestamptz NOT NULL,
  duration_ms int,
  attrs       jsonb DEFAULT '{}',
  error       text
);
CREATE INDEX IF NOT EXISTS spans_trace_idx ON spans(trace_id);

-- Upgrades a database created by an older version of this schema.
ALTER TABLE traces ADD COLUMN IF NOT EXISTS request_id text;
ALTER TABLE traces ADD COLUMN IF NOT EXISTS provider   text;
CREATE INDEX IF NOT EXISTS traces_request_idx ON traces(request_id);
