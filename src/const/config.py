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


def _list(name: str, default: str) -> list[str]:
    return [m.strip() for m in _env(name, default).split(",") if m.strip()]


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

    database_url: str = _env("DATABASE_URL", "postgresql://agent:agent@localhost:5433/agent")

    # Vector store: ChromaDB, embedded mode (a folder on disk, no server). Inspect it with tools/chroma_peek.
    chroma_path: Path = ROOT / _env("CHROMA_PATH", "chroma_data")
    chroma_collection: str = _env("CHROMA_COLLECTION", "policy_chunks")

    # Logs: logs/<dd-mm-yyyy>output.log, one JSON object per line.
    log_dir: Path = ROOT / _env("LOG_DIR", "logs")
    log_level: str = _env("LOG_LEVEL", "info")
    log_to_console: bool = _bool("LOG_TO_CONSOLE", True)

    # Idempotency-Key header: required on every POST. Replays within the TTL return the stored response.
    idempotency_ttl_s: int = _int("IDEMPOTENCY_TTL_MS", 86_400_000) // 1000

    # LLM chain: Google Gemini (primary) -> xAI Grok (fallback) -> static mode (no LLM).
    llm_max_tokens: int = _int("LLM_MAX_TOKENS", 4096)
    llm_max_retries: int = _int("LLM_MAX_RETRIES", 1)
    llm_concurrency: int = _int("LLM_CONCURRENCY", 8)
    llm_force_primary_fail: bool = _bool("LLM_FORCE_PRIMARY_FAIL", False)  # 1 = simulate a Google outage
    llm_disabled: bool = _bool("LLM_DISABLED", False)
    gemini_api_key: str = _env("GEMINI_API_KEY", "")   # empty = Gemini skipped
    # Comma list = one fallback step per model (free-tier quotas are per model, so they back each other up).
    gemini_models: list = field(default_factory=lambda: _list("GEMINI_MODEL", "gemini-2.5-flash"))
    gemini_thinking_level: str = _env("GEMINI_THINKING_LEVEL", "")  # "", minimal, low, high
    gemini_timeout_s: float = _int("GEMINI_TIMEOUT_MS", 6000) / 1000
    grok_api_key: str = _env("GROK_API_KEY", "")       # xAI key; empty = Grok skipped
    grok_base_url: str = _env("GROK_BASE_URL", "https://api.x.ai/v1")
    grok_models: list = field(default_factory=lambda: _list("GROK_MODEL", "grok-4-fast-non-reasoning"))
    grok_timeout_s: float = _int("GROK_TIMEOUT_MS", 10000) / 1000
    grok_reasoning_effort: str = _env("GROK_REASONING_EFFORT", "")  # gpt-oss on Groq: low|medium|high; empty for xAI
    llm_rate_limit_max_wait_s: float = _int("LLM_RATE_LIMIT_MAX_WAIT_MS", 12000) / 1000
    circuit_fails_to_open: int = _int("CIRCUIT_FAILS_TO_OPEN", 5)
    circuit_open_s: float = _int("CIRCUIT_OPEN_MS", 30000) / 1000

    embedding_model: str = _env("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    retrieval_top_k: int = _int("RETRIEVAL_TOP_K", 5)
    retrieval_candidates: int = _int("RETRIEVAL_CANDIDATES", 20)
    retrieval_min_vector_sim: float = _float("RETRIEVAL_MIN_VECTOR_SIM", 0.30)

    tool_timeout_s: float = _int("TOOL_TIMEOUT_MS", 2000) / 1000
    tool_max_retries: int = _int("TOOL_MAX_RETRIES", 2)
    tool_fail_rate: float = _float("TOOL_FAIL_RATE", 1 / 12)
    tool_fail_seed: str = _env("TOOL_FAIL_SEED", "")

    as_of_date: str = _env("AS_OF_DATE", "2026-11-12")
    prompt_version: str = _env("PROMPT_VERSION", "v5")
    agent_max_iterations: int = _int("AGENT_MAX_ITERATIONS", 6)
    agent_timeout_s: float = _int("AGENT_TIMEOUT_MS", 25000) / 1000
    request_timeout_s: float = _int("REQUEST_TIMEOUT_MS", 30000) / 1000
    rate_limit_per_min: int = _int("RATE_LIMIT_PER_MIN", 60)
    cache_ttl_account_s: float = _int("CACHE_TTL_ACCOUNT_MS", 0) / 1000
    answer_cache_ttl_s: float = _int("ANSWER_CACHE_TTL_MS", 900000) / 1000


config = Config()
