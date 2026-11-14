"""Idempotency-Key on every POST (Stripe-style). A POST without the header is rejected with 400.

  first request with key K        -> processed; the JSON response is stored against (tenant, K)
  retry with K and the same body  -> the stored response is returned unchanged (header Idempotent-Replayed: true),
                                     the agent is NOT run again, no second service request is raised
  retry with K and another body   -> 422 IDEMPOTENCY_KEY_REUSED (a key names one operation)
  retry while the first is running-> 409 IDEMPOTENCY_IN_PROGRESS
  server error / timeout / 429    -> key released, so the client can retry with the same key
Streaming responses (/chat/stream) are de-duplicated while in flight only (an event stream is not replayable).
If Postgres is down the request is still processed (logged as idempotency_unavailable): availability first.
"""
import json
import re

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from src.const.config import config
from src.helper.hash import sha256
from src.middleware.auth_middleware import tenant_for_key
from src.middleware.error_middleware import error_response
from src.modules.telemetry.logger import log

KEY_FORMAT = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


async def idempotency_middleware(request: Request, call_next):
    if request.method != "POST":
        return await call_next(request)
    key = request.headers.get("idempotency-key")
    if not key:
        request.state.error_code = "IDEMPOTENCY_KEY_REQUIRED"
        return error_response(request, "IDEMPOTENCY_KEY_REQUIRED")
    if not KEY_FORMAT.match(key):
        request.state.error_code = "IDEMPOTENCY_KEY_INVALID"
        return error_response(request, "IDEMPOTENCY_KEY_INVALID")

    tenant = tenant_for_key(request.headers.get("x-api-key"))
    if tenant is None:
        return await call_next(request)  # auth rejects it with 401; nothing to protect

    body_sha = sha256((await request.body()).decode("utf-8", "replace"))
    from src.db.repositories import request_log_repo as repo

    try:
        existing = await run_in_threadpool(repo.claim_key, tenant, key, request.method, request.url.path, body_sha,
                                           config.idempotency_ttl_s)
    except Exception as e:
        log("idempotency_unavailable", level="warning", error=type(e).__name__)
        return await call_next(request)

    if existing is not None:
        if existing["body_sha256"] != body_sha or existing["path"] != request.url.path:
            request.state.error_code = "IDEMPOTENCY_KEY_REUSED"
            return error_response(request, "IDEMPOTENCY_KEY_REUSED")
        if existing["state"] != "completed":
            request.state.error_code = "IDEMPOTENCY_IN_PROGRESS"
            return error_response(request, "IDEMPOTENCY_IN_PROGRESS")
        log("idempotent_replay", path=request.url.path, original_status=existing["status_code"])
        return JSONResponse(status_code=existing["status_code"], content=existing["response"],
                            headers={"idempotent-replayed": "true"})

    try:
        response = await call_next(request)
    except Exception:
        await run_in_threadpool(_release, repo, tenant, key)
        raise

    retryable = response.status_code >= 500 or response.status_code == 429
    is_json = response.headers.get("content-type", "").startswith("application/json")
    if retryable or not is_json:
        await run_in_threadpool(_release, repo, tenant, key)
        return response

    body = b"".join([chunk async for chunk in response.body_iterator])
    try:
        await run_in_threadpool(repo.complete_key, tenant, key, response.status_code, json.loads(body))
    except Exception as e:
        log("idempotency_store_failed", level="warning", error=type(e).__name__)
    headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
    return Response(content=body, status_code=response.status_code, headers=headers, media_type="application/json")


def _release(repo, tenant: str, key: str) -> None:
    try:
        repo.release_key(tenant, key)
    except Exception as e:
        log("idempotency_release_failed", level="warning", error=type(e).__name__)
