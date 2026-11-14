"""GET /requests, /requests/summary, /requests/{idempotency_key_or_trace_id}, /metrics — the req_res_log table."""
from fastapi import APIRouter, Depends, Header

from src.controller import request_controller
from src.middleware.auth_middleware import require_api_key

router = APIRouter(tags=["observability"], dependencies=[Depends(require_api_key)])


@router.get("/requests")
def list_calls(limit: int = 50, source: str | None = None, provider: str | None = None, path: str | None = None,
               status: str | None = None, flag: str | None = None):
    return request_controller.list_calls(limit, source, provider, path, status, flag)


@router.get("/requests/summary")
def summary(days: int = 7):
    return request_controller.summary(days)


@router.get("/requests/{key}")
def get_call(key: str):
    return request_controller.get_call(key)


@router.get("/metrics")
def get_metrics(accept: str | None = Header(default=None)):
    return request_controller.get_metrics(accept)
