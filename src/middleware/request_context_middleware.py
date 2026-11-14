"""Outermost middleware. For every request:
  1. request id = Idempotency-Key (if sent) else a safe x-request-id else a new uuid4
     -> request.state.request_id, the logger's requestId (ContextVar), the x-request-id response header
  2. log request_started / request_finished to logs/<dd-mm-yyyy>output.log
  3. write one row to Postgres request_logs: where it came from (ip, user agent, source), which AI provider and
     model answered (set by the chat route in request.state.agent), status code, latency. Best effort: if
     Postgres is down the request is still served and the failure is logged.
The row is written when the response body has finished streaming, so /chat/stream rows carry the model too.
"""
import re
import time
import uuid

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from src.helper.hash import sha256
from src.modules.telemetry.logger import log, reset_request_id, set_request_id

_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_UA_SOURCES = [("postman", "postman"), ("curl", "curl"), ("python", "python"), ("insomnia", "insomnia"),
               ("mozilla", "browser")]


def detect_source(headers) -> str:
    """x-client-source header wins (e.g. 'web-app', 'mobile'); otherwise derived from the User-Agent."""
    explicit = headers.get("x-client-source", "").strip().lower()
    if explicit and _SAFE_ID.match(explicit):
        return explicit[:40]
    ua = headers.get("user-agent", "").lower()
    return next((name for needle, name in _UA_SOURCES if needle in ua), "other")


def resolve_request_id(headers) -> str:
    for name in ("idempotency-key", "x-request-id"):
        value = headers.get(name, "")
        if _SAFE_ID.match(value):
            return value
    return str(uuid.uuid4())  # WHY: never echo junk ids into logs / headers


def _save_row(row: dict) -> None:
    try:
        from src.db.repositories import request_log_repo

        request_log_repo.save(row)
    except Exception as e:
        log("request_log_not_saved", level="warning", error=type(e).__name__, detail=str(e)[:160])


async def request_context_middleware(request: Request, call_next):
    t0 = time.perf_counter()
    request_id = resolve_request_id(request.headers)
    request.state.request_id = request_id
    token = set_request_id(request_id)
    source = detect_source(request.headers)
    client_ip = request.client.host if request.client else None
    log("request_started", method=request.method, path=request.url.path, source=source, client_ip=client_ip)
    try:
        response = await call_next(request)
    except Exception:
        log("request_crashed", level="error", exc_info=True, method=request.method, path=request.url.path)
        reset_request_id(token)
        raise
    response.headers["x-request-id"] = request_id

    original = response.body_iterator

    async def body_then_record():
        try:
            async for chunk in original:
                yield chunk
        finally:
            set_request_id(request_id)  # the body may stream after the middleware returned
            latency = int((time.perf_counter() - t0) * 1000)
            agent = getattr(request.state, "agent", None) or {}
            ctx = getattr(request.state, "ctx", None) or {}
            level = "error" if response.status_code >= 500 else ("warning" if response.status_code >= 400 else "info")
            log("request_finished", level=level, method=request.method, path=request.url.path,
                status_code=response.status_code, latency_ms=latency, provider=agent.get("provider"),
                model=agent.get("model_used"), agent_status=agent.get("status"))
            if not request.url.path.endswith(("/health", "/ready")):  # probes would drown the table
                await run_in_threadpool(_save_row, {
                    "request_id": request_id, "idempotency_key": request.headers.get("idempotency-key"),
                    "replayed": response.headers.get("idempotent-replayed") == "true",
                    "method": request.method, "path": request.url.path, "status_code": response.status_code,
                    "latency_ms": latency, "client_ip": client_ip,
                    "user_agent": request.headers.get("user-agent", "")[:300], "source": source,
                    "tenant_id": ctx.get("tenant_id"),
                    "api_key_hash": sha256(ctx["api_key"])[:16] if ctx.get("api_key") else None,
                    "customer_id_hash": agent.get("customer_id_hash"), "trace_id": agent.get("trace_id"),
                    "provider": agent.get("provider"), "model_used": agent.get("model_used"),
                    "fallback_used": agent.get("fallback_used"), "agent_status": agent.get("status"),
                    "error_code": getattr(request.state, "error_code", None),
                })

    response.body_iterator = body_then_record()
    reset_request_id(token)
    return response
