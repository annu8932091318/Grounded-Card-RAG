"""FastAPI app: middleware + routes wiring."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.db import index as db
from src.helper.prompt_loader import load_prompt
from src.middleware import error_middleware
from src.middleware.request_id_middleware import request_id_middleware
from src.middleware.timeout_middleware import timeout_middleware
from src.modules.telemetry.logger import log
from src.routes.index import api


@asynccontextmanager
async def lifespan(_app: FastAPI):
    prompt = load_prompt()  # fail fast if the configured prompt file is missing
    try:
        from src.db.repositories import prompt_repo

        prompt_repo.upsert_active(prompt["version"], prompt["sha256"], prompt["content"])
    except Exception as e:
        log("prompt_register_failed", level="warning", error=type(e).__name__)  # /ready will report the DB
    log("server_started", prompt_version=prompt["version"], prompt_sha=prompt["sha256"][:12])
    yield
    db.close()
    log("server_stopped")


app = FastAPI(title="Grounded Card Service Agent", version="1.0.0", lifespan=lifespan)
error_middleware.register(app)
# Starlette runs the LAST-added middleware first: request_id (outermost) -> timeout -> routes.
app.middleware("http")(timeout_middleware)
app.middleware("http")(request_id_middleware)
app.include_router(api)
