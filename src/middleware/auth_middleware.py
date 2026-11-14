"""x-api-key check. Attaches request.state.ctx = {tenant_id, api_key}.
In production customer_id would come from the authenticated session; here the body customer_id stands in."""
import hmac

from fastapi import Header, Request

from src.const.config import config
from src.const.errors import AppError


def _match(candidate: str | None, expected: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate.encode(), expected.encode())  # constant-time


def tenant_for_key(x_api_key: str | None) -> str | None:
    tenant = next((t for k, t in config.api_keys.items() if _match(x_api_key, k)), None)
    if tenant is None and _match(x_api_key, config.admin_api_key):
        tenant = "default"
    return tenant


def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> dict:
    tenant = tenant_for_key(x_api_key)
    if tenant is None:
        raise AppError("UNAUTHORIZED")
    request.state.ctx = {"tenant_id": tenant, "api_key": x_api_key}
    return request.state.ctx


def require_admin(request: Request, x_api_key: str | None = Header(default=None)) -> dict:
    if not _match(x_api_key, config.admin_api_key):
        raise AppError("UNAUTHORIZED" if not x_api_key else "FORBIDDEN")
    request.state.ctx = {"tenant_id": "default", "api_key": x_api_key, "admin": True}
    return request.state.ctx
