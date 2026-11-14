"""Token -> USD. Prices per million tokens (paid tier, standard / non-batch). These are CONFIG, not facts: verify on
the provider's pricing page before quoting a cost number (sources and date in docs/COST_REPORT.md).
Free-tier calls really cost $0; the table shows what the same traffic costs in production.
A model missing from the table reports cost 0 and logs `price_missing` once (tests/test_pricing.py fails if a model in
the configured LLM chain has no price), so the gap is visible instead of silently reading as "free".
cache_read = price of a cached input token; cache_write = surcharge for writing the cache (Anthropic only, 5-min TTL)."""
from src.modules.telemetry.logger import log

PRICES_PER_MTOK = {
    # Google Gemini (primary chain). Checked 2026-11-15 against aggregators citing ai.google.dev/pricing.
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50, "cache_read": 0.03, "cache_write": 0.0},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50, "cache_read": 0.025, "cache_write": 0.0},
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00, "cache_read": 0.05, "cache_write": 0.0},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cache_read": 0.03, "cache_write": 0.0},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40, "cache_read": 0.01, "cache_write": 0.0},
    # Groq (fallback chain): gpt-oss, cached input at 50%.
    "openai/gpt-oss-120b": {"input": 0.15, "output": 0.60, "cache_read": 0.075, "cache_write": 0.0},
    "openai/gpt-oss-20b": {"input": 0.075, "output": 0.30, "cache_read": 0.0375, "cache_write": 0.0},
    # xAI (alternative fallback).
    "grok-4-fast-non-reasoning": {"input": 0.20, "output": 0.50, "cache_read": 0.05, "cache_write": 0.0},
    # Production candidates compared in docs/COST_REPORT.md (not wired into the chain today).
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.10, "cache_write": 2.50},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
    "gpt-5-mini": {"input": 0.25, "output": 2.00, "cache_read": 0.025, "cache_write": 0.0},
    "gpt-5-nano": {"input": 0.05, "output": 0.40, "cache_read": 0.005, "cache_write": 0.0},
}
_warned: set[str] = set()


def price(model: str) -> dict | None:
    return PRICES_PER_MTOK.get(model)


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    p = price(model)
    if p is None:
        if model not in _warned:
            _warned.add(model)
            log("price_missing", level="warning", model=model, action="cost recorded as 0: add it to pricing.py")
        return 0.0
    return (input_tokens * p["input"] + output_tokens * p["output"]
            + cache_read * p["cache_read"] + cache_write * p["cache_write"]) / 1_000_000
