"""POST /admin/ingest, GET /admin/ingest/runs(/{run_id}), GET /admin/vector-store, GET|DELETE /admin/cache (admin key)"""
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


@router.get("/admin/ingest/runs/{run_id}")
def ingest_run(run_id: str):
    return admin_controller.ingest_run(run_id)


@router.get("/admin/cache")
def cache_stats():
    return admin_controller.cache_stats()


@router.delete("/admin/cache")
def cache_clear():
    return admin_controller.cache_clear()


@router.get("/admin/vector-store")
def vector_store(doc_id: str | None = None, limit: int = 50):
    return admin_controller.vector_store(doc_id, limit)
