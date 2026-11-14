"""Sets up Postgres for the app, then runs src/db/migrations/*.sql in order.

    python -m scripts.migrate                     # role + database already exist: create tables
    python -m scripts.migrate --superuser postgres # first time: asks for the postgres password, creates the
                                                   # login role + database from DATABASE_URL, then the tables

Host, port, user, password and database name all come from DATABASE_URL in .env (e.g. port 5433).
Every statement is idempotent (IF NOT EXISTS / DROP IF EXISTS), so re-running is always safe.
Postgres holds exactly three tables: req_res_log (every API call), ingest_log (every ingested document) and
semantic_cache (answers reused without the LLM). Policy vectors live in ChromaDB (./chroma_data).
"""
import argparse
import getpass
import os

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from src.const.config import config
from src.modules.telemetry.logger import log


def _target() -> dict:
    p = conninfo_to_dict(config.database_url)
    return {"host": p.get("host", "localhost"), "port": p.get("port", "5432"), "user": p.get("user"),
            "password": p.get("password"), "dbname": p.get("dbname")}


def bootstrap_with_superuser(superuser: str) -> None:
    """Create the app's login role and database using a superuser (password asked once, never stored)."""
    t = _target()
    password = os.environ.get("PGSUPERPASSWORD") or getpass.getpass(f"Password for Postgres superuser '{superuser}' "
                                                                    f"on {t['host']}:{t['port']}: ")
    admin = make_conninfo(host=t["host"], port=t["port"], user=superuser, password=password, dbname="postgres")
    with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (t["user"],)).fetchone():
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} CREATEDB").format(
                sql.Identifier(t["user"]), sql.Literal(t["password"])))
            print(f"created role {t['user']}")
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (t["dbname"],)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(t["dbname"]), sql.Identifier(t["user"])))
            print(f"created database {t['dbname']} (owner {t['user']})")
    log("database_bootstrapped", host=t["host"], port=t["port"], dbname=t["dbname"], role=t["user"])


def ensure_database() -> None:
    """Without a superuser: connect as the app role to 'postgres' and CREATE DATABASE if it is missing."""
    t = _target()
    try:
        psycopg.connect(config.database_url, connect_timeout=5).close()
        return
    except psycopg.OperationalError as e:
        msg = str(e)
        if "does not exist" not in msg or f'database "{t["dbname"]}"' not in msg:
            hint = ("Is Postgres running on that port? (pg_isready -h {host} -p {port})" if "refused" in msg or "timeout" in msg
                    else "First time? Run: python -m scripts.migrate --superuser postgres")
            raise SystemExit(f"Cannot connect with DATABASE_URL ({t['host']}:{t['port']}, user {t['user']}): "
                             f"{msg.strip().splitlines()[-1]}\n{hint.format(**t)}") from None
    with psycopg.connect(make_conninfo(config.database_url, dbname="postgres"), autocommit=True, connect_timeout=5) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(t["dbname"])))
    print(f"created database {t['dbname']}")
    log("database_created", dbname=t["dbname"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--superuser", help="create the role + database first, as this superuser (e.g. postgres)")
    args = ap.parse_args()
    if args.superuser:
        bootstrap_with_superuser(args.superuser)
    ensure_database()
    files = sorted((config.root / "src" / "db" / "migrations").glob("*.sql"))
    with psycopg.connect(config.database_url, autocommit=True) as conn:
        for f in files:
            conn.execute(f.read_text(encoding="utf-8"))
            print(f"applied {f.name}")
        tables = [r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1")]
    log("migrations_applied", files=len(files))
    t = _target()
    print(f"migrations complete ({len(files)} files) -> {t['host']}:{t['port']}/{t['dbname']}; tables: {', '.join(tables)}")


if __name__ == "__main__":
    main()
