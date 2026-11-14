"""Token -> USD. Prices per million tokens. These are CONFIG, not facts: verify on ai.google.dev/pricing and
docs.x.ai before quoting a cost number. A model missing from the table reports cost 0 (visible gap in /metrics)
rather than a guessed price. Free-tier Gemini calls are really $0; the table shows what the paid tier would cost."""

PRICES_PER_MTOK = {
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cache_read": 0.03, "cache_write": 0.0},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40, "cache_read": 0.01, "cache_write": 0.0},
    "grok-4-fast-non-reasoning": {"input": 0.20, "output": 0.50, "cache_read": 0.05, "cache_write": 0.0},
}


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    p = PRICES_PER_MTOK.get(model)
    if p is None:
        return 0.0
    return (input_tokens * p["input"] + output_tokens * p["output"]
            + cache_read * p["cache_read"] + cache_write * p["cache_write"]) / 1_000_000
