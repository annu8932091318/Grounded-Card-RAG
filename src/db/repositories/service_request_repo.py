"""service_requests — our own idempotency table (the supplied tool has no idempotency key)."""
from src.db.index import query, query_one


def find_recent(idempotency_key: str, minutes: int = 10) -> dict | None:
    return query_one(
        """SELECT request_id, category, queue, sla_hours, state FROM service_requests
           WHERE idempotency_key = %s AND created_at > now() - make_interval(mins => %s)""",
        (idempotency_key, minutes),
    )


def insert_pending(idempotency_key: str, customer_hash: str, category: str) -> None:
    # WHY: ON CONFLICT resets an *old* row for the same key (outside the 10-minute window) to pending.
    query(
        """INSERT INTO service_requests (idempotency_key, customer_id_hash, category, state)
           VALUES (%s, %s, %s, 'pending')
           ON CONFLICT (idempotency_key) DO UPDATE SET state = 'pending', request_id = NULL, created_at = now()""",
        (idempotency_key, customer_hash, category),
    )


def mark(idempotency_key: str, state: str, result: dict | None = None) -> None:
    result = result or {}
    query(
        """UPDATE service_requests SET state = %s, request_id = %s, queue = %s, sla_hours = %s
           WHERE idempotency_key = %s""",
        (state, result.get("request_id"), result.get("queue"), result.get("sla_hours"), idempotency_key),
    )
