"""Request tracking records (Postgres request_logs): where requests came from and which AI model answered."""
from src.db.repositories import request_log_repo


def list_requests(limit: int, source: str | None, provider: str | None, path: str | None) -> dict:
    return {"requests": request_log_repo.list_requests(min(max(limit, 1), 200), source, provider, path)}


def summary() -> dict:
    return request_log_repo.summary()
