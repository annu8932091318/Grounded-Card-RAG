"""GET /health (liveness), GET /ready (readiness) — no auth so orchestrators can probe them."""
from fastapi import APIRouter

from src.controller import health_controller

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    return health_controller.health()


@router.get("/ready")
def ready():
    return health_controller.ready()
