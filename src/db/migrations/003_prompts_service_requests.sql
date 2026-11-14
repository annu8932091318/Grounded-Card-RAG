CREATE TABLE IF NOT EXISTS prompt_versions (
  version    text PRIMARY KEY,                             -- 'v1'
  sha256     text NOT NULL,
  content    text NOT NULL,
  is_active  boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS service_requests (
  id               bigserial PRIMARY KEY,
  request_id       text UNIQUE,                            -- 'SR-...' from the tool
  idempotency_key  text UNIQUE NOT NULL,                   -- sha256(customer|category|summary|conversation)
  customer_id_hash text NOT NULL,
  category         text NOT NULL,
  queue            text,
  sla_hours        int,
  state            text NOT NULL CHECK (state IN ('pending','created','failed','unknown')),
  created_at       timestamptz NOT NULL DEFAULT now()
);
