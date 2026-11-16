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


def _per_model(name: str, default: str) -> dict[str, float]:
    """'gemini-embedding-2=0.89,local:...=0.87' -> {'gemini-embedding-2': 0.89, ...}. A bare number applies to all."""
    raw = _env(name, default)
    if "=" not in raw:
        return {"*": float(raw)}
    return {k.strip(): float(v) for k, v in (item.split("=", 1) for item in raw.split(",") if item.strip())}


def _per_model_pair(name: str, default: str) -> dict[str, tuple[float, float]]:
    """'model=0.30/0.16,...' -> {'model': (0.30, 0.16)} (high, low)."""
    out = {}
    for item in _env(name, default).split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            hi, _, lo = v.partition("/")
            out[k.strip()] = (float(hi), float(lo or hi))
    return out


def threshold(table: dict, model: str | None, fallback: float) -> float:
    return table.get(model or "", table.get("*", fallback))


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

    # Embeddings: first model in the chain that answers. Gemini API models first; "local:<name>" runs on CPU via
    # fastembed (no network, no quota) as the last resort. Each model has its OWN Chroma collection (vectors of
    # different models are not comparable), so a fallback model searches a corpus embedded with that same model.
    embedding_models: list = field(default_factory=lambda: _list(
        "EMBEDDING_MODEL", "gemini-embedding-001,gemini-embedding-2,local:BAAI/bge-small-en-v1.5"))
    embedding_dim: int = _int("EMBEDDING_DIM", 768)            # Gemini Matryoshka output size (768 / 1536 / 3072)
    embedding_timeout_s: float = _int("EMBEDDING_TIMEOUT_MS", 8000) / 1000
    embedding_batch_size: int = _int("EMBEDDING_BATCH_SIZE", 50)
    retrieval_top_k: int = _int("RETRIEVAL_TOP_K", 5)
    retrieval_candidates: int = _int("RETRIEVAL_CANDIDATES", 20)
    # Relevance gate ("is this question covered at all?"):
    # grounded = top sim >= HIGH  or  (top sim >= LOW and BM25 keyword coverage >= RETRIEVAL_KEYWORD_COVERAGE).
    # The gate uses RETRIEVAL_GATE_MODEL (local bge-small-en-v1.5: quota-free, milliseconds), whichever model ranked
    # the chunks. bge compresses scores (unrelated questions 0.45-0.65), so 0.65/0.55 was measured on 26 covered +
    # 16 reformulated covered + 11 uncovered probes (2026-11-15): raw covered refused 2/26 ("someone used my card at a
    # shop" 0.581, "I lost my card" 0.563, both grounded once the agent reformulates: 0.663 / 0.666), uncovered
    # grounded 2/11 (personal-loan foreclosure 0.790, home-loan rate 0.737: the citation validator still applies).
    # Thin margin: the nearest uncovered question ("EMI conversion fee" 0.646) sits 0.004 below HIGH.
    retrieval_gate_model: str = _env("RETRIEVAL_GATE_MODEL", "local:BAAI/bge-small-en-v1.5")
    retrieval_gate: dict = field(default_factory=lambda: _per_model_pair(
        "RETRIEVAL_GATE_THRESHOLDS",
        "local:BAAI/bge-small-en-v1.5=0.65/0.55,gemini-embedding-001=0.55/0.55,gemini-embedding-2=0.57/0.57"))
    retrieval_keyword_coverage: float = _float("RETRIEVAL_KEYWORD_COVERAGE", 0.34)
    # Entry scope guard (guardrails/scope_guard.py): off-domain iff top-3 similarity < MAX_SIM AND corpus-word share
    # < MAX_SHARE, per gate model ("model=max_sim/max_share"). Measured on 56 probes: 14/16 off-domain caught,
    # 0/40 in-domain blocked. A model without an entry here -> the check is skipped (fail open).
    scope_guard_enabled: bool = _bool("SCOPE_GUARD_ENABLED", True)
    scope_thresholds: dict = field(default_factory=lambda: _per_model_pair(
        "SCOPE_THRESHOLDS", "local:BAAI/bge-small-en-v1.5=0.475/0.25"))

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
    answer_cache_ttl_s: float = _int("ANSWER_CACHE_TTL_MS", 86_400_000) / 1000  # semantic_cache entry lifetime; 0 = off
    # Per embedding model (measured, paraphrase vs same-topic-different-detail): gemini-embedding-001 0.89-0.92 vs 0.81
    # -> 0.86; gemini-embedding-2 0.90-0.94 vs 0.86 -> 0.89; bge-small 0.86-0.94 vs 0.85 -> 0.90 (it
    # cannot separate 'forex markup charges?' 0.861 from '...on my Classic card?' 0.849, so it errs towards a miss).
    # A hit is then re-checked (same numbers, citations re-retrieved, output guard for the customer).
    semantic_cache_min_sim: dict = field(default_factory=lambda: _per_model(
        "SEMANTIC_CACHE_MIN_SIM", "gemini-embedding-001=0.86,gemini-embedding-2=0.89,local:BAAI/bge-small-en-v1.5=0.90"))
    semantic_cache_max_candidates: int = _int("SEMANTIC_CACHE_MAX_CANDIDATES", 2000)


config = Config()
