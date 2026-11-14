"""POST /admin/ingest, GET /admin/ingest/runs, GET /admin/vector-store (admin key only)"""
from fastapi import APIRouter, Depends

from src.controller import admin_controller
from src.middleware.auth_middleware import require_admin

router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])


@router.post("/admin/ingest")
def ingest(force: bool = False):
    return admin_controller.ingest(force)


@router.get("/admin/ingest/runs")
def ingest_runs(limit: int = 20):
    return admin_controller.ingest_runs(limit)


@router.get("/admin/vector-store")
def vector_store(doc_id: str | None = None, limit: int = 50):
    return admin_controller.vector_store(doc_id, limit)
