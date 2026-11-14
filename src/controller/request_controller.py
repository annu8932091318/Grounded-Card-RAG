"""API call records (Postgres req_res_log): who called, from where, which model answered, tokens/cost, failures."""
from fastapi.responses import PlainTextResponse

from src.const.errors import AppError
from src.db.repositories import req_res_log_repo
from src.modules.telemetry import metrics


def list_calls(limit: int, source: str | None, provider: str | None, path: str | None, status: str | None,
               flag: str | None) -> dict:
    return {"calls": req_res_log_repo.list_calls(min(max(limit, 1), 200), source, provider, path, status, flag)}


def get_call(key: str) -> dict:
    row = req_res_log_repo.get_call(key)
    if not row:
        raise AppError("NOT_FOUND")
    return row


def summary(days: int) -> dict:
    return req_res_log_repo.summary(min(max(days, 1), 365))


def get_metrics(accept: str | None):
    if accept and "text/plain" in accept:
        return PlainTextResponse(metrics.prometheus_text())
    return metrics.snapshot()
