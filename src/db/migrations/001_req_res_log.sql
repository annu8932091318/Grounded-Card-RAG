-- 1. req_res_log: ONE row per API call (and per agent turn run by scripts: source 'eval' / 'demo').
--    It is the request/response log, the idempotency store, the usage/credit ledger, the model health record
--    and the conversation memory, all keyed by the call's Idempotency-Key.
CREATE TABLE IF NOT EXISTS req_res_log (
  id                bigserial PRIMARY KEY,
  idempotency_key   text NOT NULL,                  -- Idempotency-Key header (POST) / x-request-id / generated uuid = requestId in logs
  tenant_id         text NOT NULL DEFAULT 'anonymous',
  state             text NOT NULL DEFAULT 'completed'
                    CHECK (state IN ('in_progress','completed','failed')),  -- in_progress = claimed, agent running
  -- where it came from
  method            text NOT NULL,
  path              text NOT NULL,
  source            text,                           -- x-client-source header, else postman | curl | browser | python | eval | demo | other
  client_ip         text,
  user_agent        text,
  api_key_hash      text,                           -- sha256 prefix, never the key
  customer_id_hash  text,                           -- sha256 prefix, never the raw id
  conversation_id   uuid,                           -- groups the turns of one chat (conversation memory reads these rows)
  -- what was asked / answered (PII-masked)
  request_body_sha256 text,                         -- idempotency: same key + different body -> 422
  request_body      jsonb,                          -- PII masked before storage
  response_body     jsonb,                          -- JSON response; replayed verbatim for a repeated Idempotency-Key
  status_code       int,
  error_code        text,
  agent_status      text,                           -- answered | answered_with_gap | declined | refused | request_raised | degraded
  -- how long, which model, how much
  response_time_ms  int,
  provider          text,                           -- gemini | groq | grok | NULL (static mode / cache hit / no LLM call)
  model_used        text,
  fallback_used     boolean NOT NULL DEFAULT false,
  cache_hit         boolean NOT NULL DEFAULT false, -- answered from semantic_cache, no LLM call
  llm_calls         int NOT NULL DEFAULT 0,
  llm_failures      int NOT NULL DEFAULT 0,         -- failed attempts (timeouts, 429s, errors) before an answer
  models_failed     text[] NOT NULL DEFAULT '{}',   -- which models failed in this call
  llm_attempts      jsonb,                          -- [{provider, model, attempt, ok, error, duration_ms, input_tokens, output_tokens}]
  input_tokens      int NOT NULL DEFAULT 0,         -- credits: tokens are the provider-neutral unit
  output_tokens     int NOT NULL DEFAULT 0,
  cache_read_tokens int NOT NULL DEFAULT 0,
  cost_usd          numeric(12,6) NOT NULL DEFAULT 0,  -- tokens x pricing.py (0 for free tier / unknown price)
  prompt_version    text,
  prompt_sha        text,
  flags             text[] NOT NULL DEFAULT '{}',   -- retrieval_miss, citation_repair, fallback_used, pii_masked, static_mode, ...
  trace_id          uuid,
  spans             jsonb,                          -- the agent's step tree (LLM calls, tools, retrieval, validation)
  replay_count      int NOT NULL DEFAULT 0,         -- how many times this key was replayed from the stored response
  created_at        timestamptz NOT NULL DEFAULT now(),
  completed_at      timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS req_res_log_key_uq     ON req_res_log (tenant_id, idempotency_key);
CREATE INDEX        IF NOT EXISTS req_res_log_created_idx ON req_res_log (created_at DESC);
CREATE INDEX        IF NOT EXISTS req_res_log_source_idx  ON req_res_log (source, created_at DESC);
CREATE INDEX        IF NOT EXISTS req_res_log_model_idx   ON req_res_log (provider, model_used);
CREATE INDEX        IF NOT EXISTS req_res_log_conv_idx    ON req_res_log (conversation_id, created_at);
