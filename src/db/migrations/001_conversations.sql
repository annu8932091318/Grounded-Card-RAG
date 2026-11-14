CREATE TABLE IF NOT EXISTS conversations (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),  -- gen_random_uuid is built in since Postgres 13
  tenant_id        text NOT NULL DEFAULT 'default',
  customer_id_hash text NOT NULL,                          -- never store raw ids/names
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
  id              bigserial PRIMARY KEY,
  conversation_id uuid REFERENCES conversations(id),
  role            text NOT NULL CHECK (role IN ('user','assistant')),
  content         jsonb NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_conv_idx ON messages(conversation_id, id);
