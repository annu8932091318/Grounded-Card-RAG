"""GET /traces, GET /traces/{trace_id}, GET /metrics"""
from uuid import UUID

from fastapi import APIRouter, Depends, Header

from src.controller import trace_controller
from src.middleware.auth_middleware import require_api_key

router = APIRouter(tags=["observability"], dependencies=[Depends(require_api_key)])


@router.get("/traces")
def list_traces(limit: int = 20, status: str | None = None, flag: str | None = None):
    return trace_controller.list_traces(limit, status, flag)


@router.get("/traces/{trace_id}")
def get_trace(trace_id: UUID):
    return trace_controller.get_trace(str(trace_id))


@router.get("/metrics")
def get_metrics(accept: str | None = Header(default=None)):
    return trace_controller.get_metrics(accept)
