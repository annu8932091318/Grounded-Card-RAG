"""FastAPI app: middleware + routes wiring."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.db import index as db
from src.helper.prompt_loader import load_prompt
from src.middleware import error_middleware
from src.middleware.idempotency_middleware import idempotency_middleware
from src.middleware.request_context_middleware import request_context_middleware
from src.middleware.timeout_middleware import timeout_middleware
from src.modules.telemetry.logger import attach_to, file_handler, log
from src.routes.index import api


@asynccontextmanager
async def lifespan(_app: FastAPI):
    for name in ("uvicorn.error", "uvicorn"):  # server start/stop and crash lines go to today's log file too
        attach_to(name)
    prompt = load_prompt()  # fail fast if the configured prompt file is missing (its version + sha go on every row)
    from src.modules.llm.llm_router import chain

    log("server_started", prompt_version=prompt["version"], prompt_sha=prompt["sha256"][:12],
        llm_chain=[f"{p}:{m}" for m, p in chain()] or "none (every /chat returns 503 LLM_LIMIT_REACHED)", log_file=str(file_handler.path_for_today()))
    yield
    db.close()
    log("server_stopped")


app = FastAPI(title="Grounded Card Service Agent", version="1.0.0", lifespan=lifespan)
error_middleware.register(app)
# Starlette runs the LAST-added middleware first: request_context (outermost) -> idempotency -> timeout -> routes.
app.middleware("http")(timeout_middleware)
app.middleware("http")(idempotency_middleware)
app.middleware("http")(request_context_middleware)
app.include_router(api)
