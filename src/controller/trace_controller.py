"""Traces + metrics. Customer ids appear hashed; names never."""
from fastapi.responses import PlainTextResponse

from src.const.errors import AppError
from src.db.repositories import trace_repo
from src.modules.telemetry import metrics


def list_traces(limit: int, status: str | None, flag: str | None) -> dict:
    return {"traces": trace_repo.list_traces(min(limit, 200), status, flag)}


def get_trace(trace_id: str) -> dict:
    trace = trace_repo.get_trace(trace_id)
    if not trace:
        raise AppError("NOT_FOUND")
    return trace


def get_metrics(accept: str | None):
    if accept and "text/plain" in accept:
        return PlainTextResponse(metrics.prometheus_text())
    return metrics.snapshot()
