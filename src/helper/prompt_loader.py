"""Loads the active system prompt file and its sha256 (stamped on every trace and eval run)."""
from functools import lru_cache

from src.const.config import config
from src.helper.hash import sha256


@lru_cache(maxsize=8)
def load_prompt(version: str | None = None) -> dict:
    version = version or config.prompt_version
    path = config.prompts_dir / f"system.{version}.md"
    content = path.read_text(encoding="utf-8")
    return {"version": version, "sha256": sha256(content), "content": content}


def list_prompt_files() -> list[str]:
    return sorted(p.stem.split(".", 1)[1] for p in config.prompts_dir.glob("system.*.md"))
