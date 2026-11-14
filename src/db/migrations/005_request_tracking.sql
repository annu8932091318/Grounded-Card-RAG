-- One row per HTTP request: where it came from, who sent it, what answered it, how it ended.
CREATE TABLE IF NOT EXISTS request_logs (
  id               bigserial PRIMARY KEY,
  request_id       text NOT NULL,                          -- Idempotency-Key (POST) or x-request-id / generated uuid
  idempotency_key  text,
  replayed         boolean NOT NULL DEFAULT false,         -- true = served from idempotency_keys, agent not re-run
  method           text NOT NULL,
  path             text NOT NULL,
  status_code      int,
  latency_ms       int,
  client_ip        text,                                   -- where the request came from
  user_agent       text,
  source           text,                                   -- x-client-source header, else derived: postman | curl | browser | python | other
  tenant_id        text,
  api_key_hash     text,                                   -- sha256 prefix, never the key itself
  customer_id_hash text,
  trace_id         uuid,                                   -- -> traces (agent requests only)
  provider         text,                                   -- AI provider that answered: gemini | grok | NULL
  model_used       text,                                   -- exact model id that answered
  fallback_used    boolean,
  agent_status     text,                                   -- answered | declined | refused | degraded | ...
  error_code       text,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS request_logs_created_idx ON request_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS request_logs_request_idx ON request_logs(request_id);

-- Idempotency-Key store: a retried POST with the same key + same body gets the stored response back.
CREATE TABLE IF NOT EXISTS idempotency_keys (
  key            text NOT NULL,
  tenant_id      text NOT NULL,
  method         text NOT NULL,
  path           text NOT NULL,
  body_sha256    text NOT NULL,
  state          text NOT NULL CHECK (state IN ('in_progress','completed')),
  status_code    int,
  response       jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, key)
);
CREATE INDEX IF NOT EXISTS idempotency_keys_created_idx ON idempotency_keys(created_at);
