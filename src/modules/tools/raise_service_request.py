"""Supplied tool: raise_service_request(customer_id, category, summary) -> {request_id, queue, sla_hours}.
Rejects any category that is not a POL-SVC-006 queue with InvalidCategory — it never guesses."""
import secrets

from src.const.categories import CATEGORIES
from src.modules.tools.errors import InvalidCategory


def raise_service_request(customer_id: str, category: str, summary: str) -> dict:
    if category not in CATEGORIES:
        raise InvalidCategory(category)
    return {
        "request_id": f"SR-{secrets.token_hex(4).upper()}",
        "queue": category,
        "sla_hours": CATEGORIES[category]["sla_hours"],
    }
