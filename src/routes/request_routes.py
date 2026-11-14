"""GET /requests, GET /requests/summary — the request tracking table (who called, from where, which model answered)."""
from fastapi import APIRouter, Depends

from src.controller import request_controller
from src.middleware.auth_middleware import require_api_key

router = APIRouter(tags=["requests"], dependencies=[Depends(require_api_key)])


@router.get("/requests")
def list_requests(limit: int = 50, source: str | None = None, provider: str | None = None, path: str | None = None):
    return request_controller.list_requests(limit, source, provider, path)


@router.get("/requests/summary")
def summary():
    return request_controller.summary()
