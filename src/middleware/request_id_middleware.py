"""x-request-id: reuse the caller's id or mint a uuid; echoed on every response and in error bodies."""
import re
import uuid

from fastapi import Request

_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


async def request_id_middleware(request: Request, call_next):
    incoming = request.headers.get("x-request-id", "")
    request.state.request_id = incoming if _SAFE.match(incoming) else str(uuid.uuid4())  # WHY: never echo junk
    response = await call_next(request)
    response.headers["x-request-id"] = request.state.request_id
    return response
