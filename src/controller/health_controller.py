"""Liveness and readiness. Model reachability is deliberately NOT a readiness condition (static mode exists)."""
from fastapi.responses import JSONResponse

from src.db import index as db
from src.helper.prompt_loader import load_prompt


def health() -> dict:
    return {"status": "ok"}


def ready():
    checks = {"db": db.is_up(), "corpus_ingested": False, "prompt_loaded": False}
    if checks["db"]:
        from src.db.repositories import policy_repo

        checks["corpus_ingested"] = policy_repo.stats()["chunks"] > 0
    try:
        checks["prompt_loaded"] = bool(load_prompt()["sha256"])
    except OSError:
        pass
    ok = all(checks.values())
    return JSONResponse(status_code=200 if ok else 503, content={"ready": ok, "checks": checks})
