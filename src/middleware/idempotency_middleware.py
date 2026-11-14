"""Idempotency-Key on every POST (Stripe-style), stored in req_res_log. A POST without the header -> 400.

  first call with key K              -> row (tenant, K) claimed as in_progress, call processed, row completed
                                        with the response (request_context_middleware writes it)
  retry with K and the same body     -> stored response returned unchanged (header Idempotent-Replayed: true);
                                        the agent is NOT run again, no second service request, replay_count += 1
  retry with K and a different body  -> 422 IDEMPOTENCY_KEY_REUSED (a key names one operation)
  retry while the first still runs   -> 409 IDEMPOTENCY_IN_PROGRESS
  first call failed (5xx / 429)      -> row state 'failed': the same key may be retried and is processed again
/chat/stream: the replay returns the final answer as JSON (an event stream is not replayable byte for byte).
If Postgres is down the call is still processed (logged idempotency_unavailable): availability first.
"""
import json
import re

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from src.const.config import config
from src.helper.hash import customer_hash, sha256
from src.middleware.auth_middleware import tenant_for_key
from src.middleware.error_middleware import error_response
from src.modules.guardrails import pii
from src.modules.telemetry.logger import log

KEY_FORMAT = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def masked_body(raw: bytes):
    """What req_res_log stores as request_body: PII masked, customer id hashed, never the raw text."""
    try:
        body = json.loads(raw or b"null")
    except ValueError:
        return {"_unparseable": True, "bytes": len(raw)}

    def clean(v, key=None):
        if isinstance(v, dict):
            return {k: clean(x, k) for k, x in v.items()}
        if isinstance(v, list):
            return [clean(x) for x in v]
        if key == "customer_id" and isinstance(v, str):
            return {"hash": customer_hash(v)}
        return pii.mask(v)[0] if isinstance(v, str) else v
    return clean(body)


async def idempotency_middleware(request: Request, call_next):
    if request.method != "POST":
        return await call_next(request)
    key = request.headers.get("idempotency-key")
    if not key:
        return error_response(request, "IDEMPOTENCY_KEY_REQUIRED")
    if not KEY_FORMAT.match(key):
        return error_response(request, "IDEMPOTENCY_KEY_INVALID")

    raw = await request.body()
    request.state.request_body = masked_body(raw)
    request.state.request_body_sha256 = body_sha = sha256(raw.decode("utf-8", "replace"))
    tenant = tenant_for_key(request.headers.get("x-api-key"))
    if tenant is None:
        return await call_next(request)  # auth rejects it with 401; nothing to protect
    request.state.log_tenant = tenant

    from src.db.repositories import req_res_log_repo as repo

    try:
        existing = await run_in_threadpool(repo.claim, tenant, key, request.method, request.url.path, body_sha,
                                           config.idempotency_ttl_s)
    except Exception as e:
        log("idempotency_unavailable", level="warning", error=type(e).__name__)
        return await call_next(request)

    if existing is not None:
        request.state.skip_log = True  # never overwrite the original call's row
        if existing["request_body_sha256"] != body_sha or existing["path"] != request.url.path:
            return error_response(request, "IDEMPOTENCY_KEY_REUSED")
        if existing["state"] != "completed" or existing["response_body"] is None:
            return error_response(request, "IDEMPOTENCY_IN_PROGRESS")
        await run_in_threadpool(repo.record_replay, tenant, key)
        log("idempotent_replay", path=request.url.path, original_status=existing["status_code"])
        return JSONResponse(status_code=existing["status_code"] or 200, content=existing["response_body"],
                            headers={"idempotent-replayed": "true"})

    try:
        return await call_next(request)
    except Exception:
        try:
            await run_in_threadpool(repo.mark_failed, tenant, key)
        except Exception:
            pass
        raise
