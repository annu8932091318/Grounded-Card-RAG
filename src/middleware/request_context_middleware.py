"""Outermost middleware. For every API call:
  1. requestId = Idempotency-Key (if sent) else a safe x-request-id else a new uuid4
     -> request.state.request_id, the logger's requestId (ContextVar), the x-request-id response header
  2. log request_started / request_finished to logs/<dd-mm-yyyy>output.log
  3. write ONE row to req_res_log when the response body has finished (so /chat/stream rows are complete too):
     where it came from (ip, user agent, source), request (PII-masked) and response, response time, status,
     and from the agent: provider + model, every LLM attempt (ok / error), tokens, cost, flags, spans.
     Best effort: if Postgres is down the call is still served and the failure is logged.
"""
import json
import re
import time
import uuid

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from src.helper.hash import sha256
from src.modules.telemetry.logger import in_http_var, log, reset_request_id, set_request_id

_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_UA_SOURCES = [("postman", "postman"), ("curl", "curl"), ("python", "python"), ("insomnia", "insomnia"),
               ("mozilla", "browser")]
_MAX_LOGGED_BODY = 256 * 1024
_NOT_LOGGED = ("/health", "/ready", "/docs", "/openapi.json")  # probes and docs would drown the table


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
        from src.db.repositories import req_res_log_repo

        req_res_log_repo.upsert(row)
    except Exception as e:
        log("req_res_log_write_failed", level="warning", error=type(e).__name__, detail=str(e)[:200])


async def request_context_middleware(request: Request, call_next):
    t0 = time.perf_counter()
    request_id = resolve_request_id(request.headers)
    request.state.request_id = request_id
    token = set_request_id(request_id)
    http_token = in_http_var.set(True)
    source = detect_source(request.headers)
    client_ip = request.client.host if request.client else None
    log("request_started", method=request.method, path=request.url.path, source=source, client_ip=client_ip)
    try:
        response = await call_next(request)
    except Exception:
        log("request_crashed", level="error", exc_info=True, method=request.method, path=request.url.path)
        reset_request_id(token)
        in_http_var.reset(http_token)
        raise
    response.headers["x-request-id"] = request_id
    is_json = response.headers.get("content-type", "").startswith("application/json")
    original = response.body_iterator

    async def body_then_record():
        captured, size = [], 0
        try:
            async for chunk in original:
                if is_json and size < _MAX_LOGGED_BODY:
                    captured.append(chunk)
                    size += len(chunk)
                yield chunk
        finally:
            set_request_id(request_id)  # the body may stream after the middleware returned
            latency = int((time.perf_counter() - t0) * 1000)
            agent = dict(getattr(request.state, "agent", None) or {})
            level = "error" if response.status_code >= 500 else ("warning" if response.status_code >= 400 else "info")
            log("request_finished", level=level, method=request.method, path=request.url.path,
                status_code=response.status_code, response_time_ms=latency, provider=agent.get("provider"),
                model=agent.get("model_used"), agent_status=agent.get("agent_status"),
                llm_failures=agent.get("llm_failures"), cache_hit=agent.get("cache_hit"))
            if not request.url.path.endswith(_NOT_LOGGED) and not getattr(request.state, "skip_log", False):
                response_body = agent.pop("response_body", None)
                if is_json and captured:
                    try:
                        response_body = json.loads(b"".join(captured))
                    except ValueError:
                        pass
                message = agent.pop("request_message", None)
                ctx = getattr(request.state, "ctx", None) or {}
                request_body = getattr(request.state, "request_body", None)
                if message is not None and isinstance(request_body, dict):
                    request_body = {**request_body, "message": message}  # the agent's PII-masked, sanitized text
                if request.method == "GET" and request.query_params:
                    request_body = {"query": dict(request.query_params)}
                failed = response.status_code >= 500 or response.status_code == 429
                await run_in_threadpool(_save_row, {
                    **agent,
                    "idempotency_key": request_id,
                    "tenant_id": getattr(request.state, "log_tenant", None) or ctx.get("tenant_id") or "anonymous",
                    "state": "failed" if failed else "completed",
                    "method": request.method, "path": request.url.path, "source": source, "client_ip": client_ip,
                    "user_agent": request.headers.get("user-agent", "")[:300],
                    "api_key_hash": sha256(ctx["api_key"])[:16] if ctx.get("api_key") else None,
                    "request_body_sha256": getattr(request.state, "request_body_sha256", None),
                    "request_body": request_body, "response_body": response_body,
                    "status_code": response.status_code, "response_time_ms": latency,
                    "error_code": getattr(request.state, "error_code", None) or agent.get("error_code"),
                })

    response.body_iterator = body_then_record()
    reset_request_id(token)
    in_http_var.reset(http_token)
    return response
