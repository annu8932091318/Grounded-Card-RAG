"""conversations / messages. Only hashed customer ids are stored."""
import json

from src.db.index import query, query_one


def get_or_create(conversation_id: str | None, tenant: str, customer_hash: str) -> str:
    if conversation_id:
        row = query_one(
            "SELECT id FROM conversations WHERE id = %s AND tenant_id = %s AND customer_id_hash = %s",
            (conversation_id, tenant, customer_hash),
        )
        if row:
            return str(row["id"])
    # WHY: an unknown or foreign conversation id silently starts a new one — never attach to another customer's thread.
    row = query_one(
        "INSERT INTO conversations (tenant_id, customer_id_hash) VALUES (%s, %s) RETURNING id",
        (tenant, customer_hash),
    )
    return str(row["id"])


def add_message(conversation_id: str, role: str, content: dict) -> None:
    query(
        "INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s::jsonb)",
        (conversation_id, role, json.dumps(content)),
    )


def recent_messages(conversation_id: str, limit: int = 6) -> list[dict]:
    rows = query(
        "SELECT role, content FROM messages WHERE conversation_id = %s ORDER BY id DESC LIMIT %s",
        (conversation_id, limit),
    )
    return list(reversed(rows))
