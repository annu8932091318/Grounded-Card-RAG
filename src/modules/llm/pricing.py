"""Token -> USD. Prices per million tokens, from Anthropic's published model pricing (checked 2026-11).
Sonnet 5.5: $2 in / $10 out / $0.10 cache read. Haiku 5.5: $0.10 in / $0.50 out.
Cache-write and Haiku cache-read prices use the standard multipliers (1.25x / 0.1x input) — VERIFY on the
price page before quoting cost numbers; they are config, not code."""

PRICES_PER_MTOK = {
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.10, "cache_write": 2.50},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
}


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    p = PRICES_PER_MTOK.get(model)
    if p is None:
        return 0.0  # unknown model: report 0 rather than guess (shows up as a gap in /metrics)
    return (input_tokens * p["input"] + output_tokens * p["output"]
            + cache_read * p["cache_read"] + cache_write * p["cache_write"]) / 1_000_000
