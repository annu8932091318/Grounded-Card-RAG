"""req_res_log: one row per API call. Idempotency store + request/response log + usage ledger + model health
+ conversation memory, keyed by (tenant_id, idempotency_key)."""
import json

from src.db.index import query, query_one

# Columns written at the end of a call (the claim row only has key, method, path, body hash).
COLUMNS = [
    "idempotency_key", "tenant_id", "state", "method", "path", "source", "client_ip", "user_agent", "api_key_hash",
    "customer_id_hash", "conversation_id", "request_body_sha256", "request_body", "response_body", "status_code",
    "error_code", "agent_status", "response_time_ms", "provider", "model_used", "fallback_used", "cache_hit",
    "llm_calls", "llm_failures", "models_failed", "llm_attempts", "input_tokens", "output_tokens", "cache_read_tokens",
    "cost_usd", "prompt_version", "prompt_sha", "flags", "trace_id", "spans",
]
_JSON = {"request_body", "response_body", "llm_attempts", "spans"}
_DEFAULTS = {"state": "completed", "tenant_id": "anonymous", "fallback_used": False, "cache_hit": False, "llm_calls": 0,
             "llm_failures": 0, "models_failed": [], "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
             "cost_usd": 0, "flags": []}


def _params(row: dict) -> dict:
    out = {}
    for c in COLUMNS:
        v = row.get(c, _DEFAULTS.get(c))
        if v is None:
            v = _DEFAULTS.get(c)
        out[c] = json.dumps(v, default=str) if c in _JSON and v is not None else v
    return out


def upsert(row: dict) -> None:
    """Write the finished call. Updates the row claimed at the start (POST) or inserts a new one (GET, scripts)."""
    cols = ", ".join(COLUMNS)
    vals = ", ".join(f"%({c})s::jsonb" if c in _JSON else f"%({c})s" for c in COLUMNS)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c not in ("idempotency_key", "tenant_id"))
    query(f"""INSERT INTO req_res_log ({cols}, completed_at) VALUES ({vals}, now())
              ON CONFLICT (tenant_id, idempotency_key) DO UPDATE SET {updates}, completed_at = now()""", _params(row))


# --- idempotency ----------------------------------------------------------------------------------------------

def claim(tenant: str, key: str, method: str, path: str, body_sha: str, ttl_s: int) -> dict | None:
    """Claim (tenant, key) for this call. Returns None when we own it (new key, or the previous attempt failed /
    expired), else the existing row (-> replay, 409 or 422). One statement, so concurrent duplicates can't both win."""
    owned = query_one(
        """INSERT INTO req_res_log (idempotency_key, tenant_id, state, method, path, request_body_sha256)
           VALUES (%(key)s, %(tenant)s, 'in_progress', %(method)s, %(path)s, %(sha)s)
           ON CONFLICT (tenant_id, idempotency_key) DO UPDATE
             SET state = 'in_progress', method = EXCLUDED.method, path = EXCLUDED.path,
                 request_body_sha256 = EXCLUDED.request_body_sha256, response_body = NULL, replay_count = 0,
                 created_at = now(), completed_at = NULL
             WHERE req_res_log.state = 'failed'
                OR req_res_log.created_at < now() - make_interval(secs => %(ttl)s)
           RETURNING id""",
        {"key": key, "tenant": tenant, "method": method, "path": path, "sha": body_sha, "ttl": ttl_s},
    )
    if owned:
        return None
    return query_one("""SELECT state, path, request_body_sha256, status_code, response_body
                        FROM req_res_log WHERE tenant_id = %s AND idempotency_key = %s""", (tenant, key))


def record_replay(tenant: str, key: str) -> None:
    query("UPDATE req_res_log SET replay_count = replay_count + 1 WHERE tenant_id = %s AND idempotency_key = %s",
          (tenant, key))


def mark_failed(tenant: str, key: str) -> None:
    """The call crashed before a row could be finished: free the key so the client can retry it."""
    query("UPDATE req_res_log SET state = 'failed', completed_at = now() WHERE tenant_id = %s AND idempotency_key = %s "
          "AND state = 'in_progress'", (tenant, key))


# --- conversation memory --------------------------------------------------------------------------------------

def conversation_owner(conversation_id: str) -> str | None:
    row = query_one("SELECT customer_id_hash FROM req_res_log WHERE conversation_id = %s AND customer_id_hash IS NOT NULL "
                    "ORDER BY created_at LIMIT 1", (conversation_id,))
    return row["customer_id_hash"] if row else None


def recent_turns(conversation_id: str, customer_hash: str, limit: int = 2) -> list[dict]:
    """Last finished turns of this conversation for this customer: masked question + customer_message."""
    rows = query(
        """SELECT request_body->>'message' AS question, response_body->>'customer_message' AS answer
           FROM req_res_log
           WHERE conversation_id = %s AND customer_id_hash = %s AND state = 'completed' AND agent_status IS NOT NULL
           ORDER BY created_at DESC LIMIT %s""",
        (conversation_id, customer_hash, limit),
    )
    return list(reversed(rows))


# --- reading ---------------------------------------------------------------------------------------------------

_LIST_COLS = """idempotency_key, tenant_id, state, method, path, source, client_ip, status_code, error_code, agent_status,
                response_time_ms, provider, model_used, fallback_used, cache_hit, llm_calls, llm_failures, models_failed,
                input_tokens, output_tokens, cost_usd, prompt_version, flags, replay_count, trace_id, conversation_id,
                created_at"""


def list_calls(limit: int = 50, source: str | None = None, provider: str | None = None, path: str | None = None,
               status: str | None = None, flag: str | None = None) -> list[dict]:
    return query(
        f"""SELECT {_LIST_COLS} FROM req_res_log
            WHERE (%(source)s::text IS NULL OR source = %(source)s)
              AND (%(provider)s::text IS NULL OR provider = %(provider)s)
              AND (%(path)s::text IS NULL OR path LIKE %(path)s || '%%')
              AND (%(status)s::text IS NULL OR agent_status = %(status)s)
              AND (%(flag)s::text IS NULL OR %(flag)s = ANY(flags))
            ORDER BY created_at DESC LIMIT %(limit)s""",
        {"source": source, "provider": provider, "path": path, "status": status, "flag": flag, "limit": limit},
    )


def get_call(key: str) -> dict | None:
    """Full row (request, response, LLM attempts, spans) by Idempotency-Key / requestId or trace id."""
    return query_one("SELECT * FROM req_res_log WHERE idempotency_key = %s OR trace_id::text = %s "
                     "ORDER BY created_at DESC LIMIT 1", (key, key))


def summary(days: int = 7) -> dict:
    """Who uses how much (credits = tokens + cost) and which model fails or misbehaves."""
    window = {"days": days}
    by_source = query(
        """SELECT coalesce(source, 'unknown') AS source, count(*) AS calls, sum(input_tokens) AS input_tokens,
                  sum(output_tokens) AS output_tokens, round(sum(cost_usd), 6) AS cost_usd,
                  round(avg(response_time_ms)) AS avg_response_ms, sum(cache_hit::int) AS cache_hits,
                  sum(llm_calls) AS llm_calls
           FROM req_res_log WHERE created_at > now() - make_interval(days => %(days)s)
           GROUP BY 1 ORDER BY calls DESC""", window)
    by_model = query(
        """SELECT a->>'provider' AS provider, a->>'model' AS model, count(*) AS attempts,
                  sum(((a->>'ok')::boolean)::int) AS ok,
                  count(*) - sum(((a->>'ok')::boolean)::int) AS failed,
                  round(100.0 * (count(*) - sum(((a->>'ok')::boolean)::int)) / count(*), 1) AS failure_pct,
                  round(avg((a->>'duration_ms')::int)) AS avg_ms,
                  sum(coalesce((a->>'input_tokens')::int, 0)) AS input_tokens,
                  sum(coalesce((a->>'output_tokens')::int, 0)) AS output_tokens
           FROM (SELECT jsonb_array_elements(llm_attempts) AS a FROM req_res_log
                 WHERE llm_attempts IS NOT NULL AND created_at > now() - make_interval(days => %(days)s)) x
           GROUP BY 1, 2 ORDER BY attempts DESC""", window)
    errors = query(
        """SELECT a->>'model' AS model, a->>'error' AS error, count(*) AS n
           FROM (SELECT jsonb_array_elements(llm_attempts) AS a FROM req_res_log
                 WHERE llm_attempts IS NOT NULL AND created_at > now() - make_interval(days => %(days)s)) x
           WHERE NOT (a->>'ok')::boolean GROUP BY 1, 2 ORDER BY n DESC""", window)
    for m in by_model:
        m["errors"] = {e["error"]: e["n"] for e in errors if e["model"] == m["model"]}
    behaviour = query(
        """SELECT coalesce(model_used, '(static / cache)') AS model_used, count(*) AS answers,
                  sum(('citation_repair' = ANY(flags))::int) AS needed_repair,
                  sum(('citation_validation_failed' = ANY(flags))::int) AS failed_validation,
                  sum(('leak_blocked' = ANY(flags))::int) AS guard_blocked,
                  sum((agent_status = 'degraded')::int) AS degraded,
                  round(avg(response_time_ms)) AS avg_response_ms
           FROM req_res_log WHERE agent_status IS NOT NULL AND created_at > now() - make_interval(days => %(days)s)
           GROUP BY 1 ORDER BY answers DESC""", window)
    by_status = query("""SELECT status_code, count(*) AS calls FROM req_res_log
                         WHERE created_at > now() - make_interval(days => %(days)s) GROUP BY 1 ORDER BY 1""", window)
    return {"window_days": days, "usage_by_source": by_source, "llm_attempts_by_model": by_model,
            "answer_quality_by_model": behaviour, "calls_by_status_code": by_status}
