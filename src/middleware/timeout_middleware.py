"""Hard request timeout. The agent has its own 25 s budget; this is the outer safety net (REQUEST_TIMEOUT_MS)."""
import asyncio

from fastapi import Request

from src.const.config import config
from src.middleware.error_middleware import error_response


async def timeout_middleware(request: Request, call_next):
    if request.url.path.endswith("/eval/run"):
        return await call_next(request)  # WHY: an eval run is a long admin job, not a customer request
    try:
        return await asyncio.wait_for(call_next(request), timeout=config.request_timeout_s)
    except asyncio.TimeoutError:
        return error_response(request, "TIMEOUT")
