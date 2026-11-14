"""prompt_versions — one row per prompt file version; exactly one active."""
from src.db.index import query, query_one, transaction


def upsert_active(version: str, sha: str, content: str) -> None:
    with transaction() as conn:
        conn.execute("UPDATE prompt_versions SET is_active = false WHERE version <> %s", (version,))
        conn.execute(
            """INSERT INTO prompt_versions (version, sha256, content, is_active) VALUES (%s, %s, %s, true)
               ON CONFLICT (version) DO UPDATE SET sha256 = EXCLUDED.sha256, content = EXCLUDED.content, is_active = true""",
            (version, sha, content),
        )


def list_versions() -> list[dict]:
    return query("SELECT version, sha256, is_active, created_at FROM prompt_versions ORDER BY version")


def active() -> dict | None:
    return query_one("SELECT version, sha256, is_active FROM prompt_versions WHERE is_active")
