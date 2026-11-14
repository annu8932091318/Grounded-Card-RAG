"""env -> typed config. The single place that reads os.environ."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _env(name: str, default: str) -> str:
    # WHY: .env lines may carry trailing "# comments"; strip them so values stay clean.
    return os.environ.get(name, default).split(" #")[0].strip()


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes")


def _api_keys() -> dict[str, str]:
    """'k1,k2:tenantB' -> {'k1': 'default', 'k2': 'tenantB'}"""
    keys = {}
    for item in _env("API_KEYS", "demo-key-1").split(","):
        if item.strip():
            key, _, tenant = item.strip().partition(":")
            keys[key] = tenant or "default"
    return keys


@dataclass
class Config:
    # WHY: not frozen — the eval runner and demo override tool_fail_rate per case (spec §15.1 env column).
    root: Path = ROOT
    data_dir: Path = ROOT / "data"
    prompts_dir: Path = ROOT / "prompts"
    port: int = _int("PORT", 3000)
    env: str = _env("ENV", "development")
    api_keys: dict = field(default_factory=_api_keys)
    admin_api_key: str = _env("ADMIN_API_KEY", "admin-key-1")

    database_url: str = _env("DATABASE_URL", "postgresql://agent:agent@localhost:5432/agent")

    primary_model: str = _env("PRIMARY_MODEL", "claude-sonnet-5-5")
    fallback_model: str = _env("FALLBACK_MODEL", "claude-haiku-5-5")
    llm_effort: str = _env("LLM_EFFORT", "medium")
    llm_max_tokens: int = _int("LLM_MAX_TOKENS", 4096)
    llm_timeout_s: float = _int("LLM_TIMEOUT_MS", 20000) / 1000
    llm_max_retries: int = _int("LLM_MAX_RETRIES", 1)
    llm_concurrency: int = _int("LLM_CONCURRENCY", 8)
    llm_strict_tools: bool = _bool("LLM_STRICT_TOOLS", True)
    llm_server_fallback: bool = _bool("LLM_SERVER_FALLBACK", True)
    llm_force_primary_fail: bool = _bool("LLM_FORCE_PRIMARY_FAIL", False)
    llm_disabled: bool = _bool("LLM_DISABLED", False)
    circuit_fails_to_open: int = _int("CIRCUIT_FAILS_TO_OPEN", 5)
    circuit_open_s: float = _int("CIRCUIT_OPEN_MS", 30000) / 1000

    embedding_model: str = _env("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    embedding_dim: int = _int("EMBEDDING_DIM", 384)
    retrieval_top_k: int = _int("RETRIEVAL_TOP_K", 5)
    retrieval_candidates: int = _int("RETRIEVAL_CANDIDATES", 20)
    retrieval_min_vector_sim: float = _float("RETRIEVAL_MIN_VECTOR_SIM", 0.30)

    tool_timeout_s: float = _int("TOOL_TIMEOUT_MS", 2000) / 1000
    tool_max_retries: int = _int("TOOL_MAX_RETRIES", 2)
    tool_fail_rate: float = _float("TOOL_FAIL_RATE", 1 / 12)
    tool_fail_seed: str = _env("TOOL_FAIL_SEED", "")

    as_of_date: str = _env("AS_OF_DATE", "2026-11-12")
    prompt_version: str = _env("PROMPT_VERSION", "v1")
    agent_max_iterations: int = _int("AGENT_MAX_ITERATIONS", 6)
    agent_timeout_s: float = _int("AGENT_TIMEOUT_MS", 25000) / 1000
    request_timeout_s: float = _int("REQUEST_TIMEOUT_MS", 30000) / 1000
    rate_limit_per_min: int = _int("RATE_LIMIT_PER_MIN", 60)
    cache_ttl_account_s: float = _int("CACHE_TTL_ACCOUNT_MS", 0) / 1000
    answer_cache_ttl_s: float = _int("ANSWER_CACHE_TTL_MS", 900000) / 1000


config = Config()
