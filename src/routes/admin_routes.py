"""POST /admin/ingest (admin key only)"""
from fastapi import APIRouter, Depends

from src.controller import admin_controller
from src.middleware.auth_middleware import require_admin

router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])


@router.post("/admin/ingest")
def ingest():
    return admin_controller.ingest()
