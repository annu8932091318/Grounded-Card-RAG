"""Liveness and readiness. Model reachability is deliberately NOT a readiness condition (a missing model is a 503 per request, not a dead service),
but the configured chain is reported so a missing API key is visible at a glance."""
from fastapi.responses import JSONResponse

from src.db import chroma
from src.db import index as db
from src.helper.prompt_loader import load_prompt


def health() -> dict:
    return {"status": "ok"}


def ready():
    checks = {"postgres": db.is_up(), "chroma": chroma.is_up(), "corpus_ingested": False, "prompt_loaded": False}
    if checks["chroma"]:
        from src.db.repositories import policy_repo

        checks["corpus_ingested"] = policy_repo.stats()["chunks"] > 0
    try:
        checks["prompt_loaded"] = bool(load_prompt()["sha256"])
    except OSError:
        pass
    from src.modules.llm.llm_router import chain

    ok = all(checks.values())
    return JSONResponse(status_code=200 if ok else 503, content={
        "ready": ok, "checks": checks,
        "llm_chain": [f"{provider}:{model}" for model, provider in chain()] or "none configured: /chat returns 503 LLM_LIMIT_REACHED"})
