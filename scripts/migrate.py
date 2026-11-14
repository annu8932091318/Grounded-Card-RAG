"""Runs src/db/migrations/*.sql in order. Every statement is idempotent (IF NOT EXISTS), so re-running is safe.
Usage: python -m scripts.migrate"""
import psycopg

from src.const.config import config


def main() -> None:
    files = sorted((config.root / "src" / "db" / "migrations").glob("*.sql"))
    with psycopg.connect(config.database_url, autocommit=True) as conn:
        for f in files:
            conn.execute(f.read_text(encoding="utf-8"))
            print(f"applied {f.name}")
    print(f"migrations complete ({len(files)} files)")


if __name__ == "__main__":
    main()
