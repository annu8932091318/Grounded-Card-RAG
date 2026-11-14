"""Mounts every route group under /api/v1."""
from fastapi import APIRouter

from src.routes import admin_routes, chat_routes, eval_routes, health_routes, prompt_routes, retrieval_routes, trace_routes

api = APIRouter(prefix="/api/v1")
for module in (chat_routes, retrieval_routes, trace_routes, eval_routes, prompt_routes, admin_routes, health_routes):
    api.include_router(module.router)
