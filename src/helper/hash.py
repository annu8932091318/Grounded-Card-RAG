"""sha256 helpers: prompt sha, hashed customer ids for logs, idempotency keys."""
import hashlib


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def customer_hash(customer_id: str) -> str:
    # WHY: logs/traces must be joinable per customer without storing the raw id.
    return sha256(f"customer:{customer_id}")[:16]


def idempotency_key(customer_id: str, category: str, summary: str, conversation_id: str) -> str:
    return sha256("|".join([customer_id, category, summary.strip().lower(), conversation_id or ""]))
