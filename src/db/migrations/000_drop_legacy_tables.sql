-- The schema is now three tables (req_res_log, ingest_log, semantic_cache).
-- Earlier versions had these; they are removed (their data lives in req_res_log, eval/results/*.json, files).
DROP TABLE IF EXISTS spans, traces, messages, conversations, service_requests, prompt_versions,
                     eval_results, eval_runs, request_logs, idempotency_keys, ingest_runs, schema_migrations,
                     policy_chunks, policy_docs CASCADE;
