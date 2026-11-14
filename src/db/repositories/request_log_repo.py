"""request_logs — one row per HTTP request (source, model used, outcome) — and the idempotency_keys store."""
import json

from src.db.index import query, query_one

_COLUMNS = ["request_id", "idempotency_key", "replayed", "method", "path", "status_code", "latency_ms", "client_ip",
            "user_agent", "source", "tenant_id", "api_key_hash", "customer_id_hash", "trace_id", "provider",
            "model_used", "fallback_used", "agent_status", "error_code"]


def save(row: dict) -> None:
    cols = ", ".join(_COLUMNS)
    values = ", ".join(f"%({c})s" for c in _COLUMNS)
    query(f"INSERT INTO request_logs ({cols}) VALUES ({values})", {c: row.get(c) for c in _COLUMNS})


def list_requests(limit: int = 50, source: str | None = None, provider: str | None = None,
                  path: str | None = None) -> list[dict]:
    return query(
        """SELECT * FROM request_logs
           WHERE (%(source)s::text IS NULL OR source = %(source)s)
             AND (%(provider)s::text IS NULL OR provider = %(provider)s)
             AND (%(path)s::text IS NULL OR path LIKE %(path)s || '%%')
           ORDER BY created_at DESC LIMIT %(limit)s""",
        {"source": source, "provider": provider, "path": path, "limit": limit},
    )


def summary() -> dict:
    by_source = query("SELECT source, count(*) AS n FROM request_logs GROUP BY source ORDER BY n DESC")
    by_model = query("""SELECT provider, model_used, count(*) AS n, round(avg(latency_ms)) AS avg_latency_ms
                        FROM request_logs WHERE model_used IS NOT NULL GROUP BY provider, model_used ORDER BY n DESC""")
    by_status = query("SELECT status_code, count(*) AS n FROM request_logs GROUP BY status_code ORDER BY status_code")
    return {"by_source": by_source, "by_model": by_model, "by_status_code": by_status}


# --- idempotency ---------------------------------------------------------------------------------------------

def claim_key(tenant: str, key: str, method: str, path: str, body_sha: str, ttl_s: int) -> dict | None:
    """Try to claim the key. Returns None if we own it now (process the request), else the existing row.
    Expired rows (older than the TTL) are replaced. One statement, so two concurrent requests cannot both win."""
    query("DELETE FROM idempotency_keys WHERE tenant_id = %s AND key = %s AND created_at < now() - make_interval(secs => %s)",
          (tenant, key, ttl_s))
    inserted = query_one(
        """INSERT INTO idempotency_keys (key, tenant_id, method, path, body_sha256, state)
           VALUES (%s, %s, %s, %s, %s, 'in_progress')
           ON CONFLICT (tenant_id, key) DO NOTHING RETURNING key""",
        (key, tenant, method, path, body_sha),
    )
    if inserted:
        return None
    return query_one("SELECT * FROM idempotency_keys WHERE tenant_id = %s AND key = %s", (tenant, key))


def complete_key(tenant: str, key: str, status_code: int, response: dict) -> None:
    query("UPDATE idempotency_keys SET state = 'completed', status_code = %s, response = %s::jsonb WHERE tenant_id = %s AND key = %s",
          (status_code, json.dumps(response, default=str), tenant, key))


def release_key(tenant: str, key: str) -> None:
    """The request failed before producing a storable response: let the client retry with the same key."""
    query("DELETE FROM idempotency_keys WHERE tenant_id = %s AND key = %s AND state = 'in_progress'", (tenant, key))
