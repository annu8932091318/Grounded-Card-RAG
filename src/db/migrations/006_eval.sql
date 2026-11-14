CREATE TABLE IF NOT EXISTS eval_runs (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  started_at          timestamptz DEFAULT now(),
  git_sha             text, prompt_version text, model text,
  total               int, passed int, expected_fail_total int, expected_fail_hit int,
  unexpected_fail     int, critical_fail int, pass_rate numeric(5,2),
  gate_passed         boolean
);

CREATE TABLE IF NOT EXISTS eval_results (
  run_id          uuid REFERENCES eval_runs(id) ON DELETE CASCADE,
  case_id         text, passed boolean, expect_fail boolean, severity text,
  actual_status   text, failure_reasons jsonb,
  latency_ms      int, input_tokens int, output_tokens int,
  trace_id        uuid,
  PRIMARY KEY (run_id, case_id)
);
