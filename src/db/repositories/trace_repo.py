"""traces / spans."""
import json

from src.db.index import query, query_one, transaction


def save(trace: dict, spans: list[dict]) -> None:
    with transaction() as conn:
        conn.execute(
            """INSERT INTO traces (trace_id, request_id, conversation_id, tenant_id, customer_id_hash, started_at, latency_ms,
                 status, prompt_version, prompt_sha, provider, model_used, fallback_used, llm_calls, input_tokens,
                 output_tokens, cache_read_tokens, cost_usd, cache_hit, validation_retries, error_code, flags)
               VALUES (%(trace_id)s, %(request_id)s, %(conversation_id)s, %(tenant_id)s, %(customer_id_hash)s, %(started_at)s,
                 %(latency_ms)s, %(status)s, %(prompt_version)s, %(prompt_sha)s, %(provider)s, %(model_used)s, %(fallback_used)s,
                 %(llm_calls)s, %(input_tokens)s, %(output_tokens)s, %(cache_read_tokens)s, %(cost_usd)s,
                 %(cache_hit)s, %(validation_retries)s, %(error_code)s, %(flags)s)""",
            trace,
        )
        for s in spans:
            conn.execute(
                """INSERT INTO spans (span_id, trace_id, parent_id, name, started_at, duration_ms, attrs, error)
                   VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)""",
                (s["span_id"], trace["trace_id"], s["parent_id"], s["name"], s["started_at"],
                 s["duration_ms"], json.dumps(s["attrs"], default=str), s["error"]),
            )


def list_traces(limit: int = 20, status: str | None = None, flag: str | None = None) -> list[dict]:
    return query(
        """SELECT trace_id, request_id, started_at, latency_ms, status, prompt_version, provider, model_used, fallback_used,
                  llm_calls, input_tokens, output_tokens, cache_read_tokens, cost_usd, error_code, flags,
                  customer_id_hash
           FROM traces
           WHERE (%(status)s::text IS NULL OR status = %(status)s)
             AND (%(flag)s::text IS NULL OR %(flag)s = ANY(flags))
           ORDER BY started_at DESC LIMIT %(limit)s""",
        {"status": status, "flag": flag, "limit": limit},
    )


def get_trace(trace_id: str) -> dict | None:
    trace = query_one("SELECT * FROM traces WHERE trace_id = %s", (trace_id,))
    if trace:
        trace["spans"] = query("SELECT * FROM spans WHERE trace_id = %s ORDER BY started_at", (trace_id,))
    return trace
