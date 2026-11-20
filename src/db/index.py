"""Postgres pool + tiny query helpers. Postgres stores tracking records only; vectors live in ChromaDB."""
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from src.const.config import config

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        # WHY lazy: unit tests must import modules without a running database.
        # WHY short timeouts: if Postgres is down, telemetry/conversation writes must fail fast, not stall answers.
        _pool = ConnectionPool(config.database_url, min_size=1, max_size=10, timeout=5, open=True,
                               kwargs={"row_factory": dict_row, "connect_timeout": 3})
    return _pool


def query(sql: str, params: tuple | dict = ()) -> list[dict]:
    with pool().connection() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def query_one(sql: str, params: tuple | dict = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


@contextmanager
def transaction():
    """Yields a connection; commits on success, rolls back on exception."""
    with pool().connection() as conn:
        with conn.transaction():
            yield conn


def is_up() -> bool:
    try:
        query("SELECT 1")
        return True
    except Exception:
        return False


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
