"""Prompt versions: files on disk are the source; the DB row records what was active (and its sha)."""
from src.helper.prompt_loader import list_prompt_files, load_prompt


def list_prompts() -> dict:
    active = load_prompt()["version"]
    return {"prompts": [{"version": v, "sha256": load_prompt(v)["sha256"], "is_active": v == active}
                        for v in list_prompt_files()]}


def active_prompt() -> dict:
    p = load_prompt()
    return {"version": p["version"], "sha256": p["sha256"], "is_active": True}
