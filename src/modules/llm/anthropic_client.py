"""One Messages API call with timeout, prompt caching and usage capture. Retries/fallback live in llm_router."""
import anthropic

from src.const.config import config

_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        # WHY max_retries=0: llm_router owns retry + fallback so every attempt is visible as a span.
        _client = anthropic.Anthropic(timeout=config.llm_timeout_s, max_retries=0)
    return _client


def call(model: str, system: str, tools: list[dict], messages: list) -> object:
    kwargs = dict(
        model=model,
        max_tokens=config.llm_max_tokens,
        # Prompt caching: tools + system are a stable prefix (render order tools -> system -> messages).
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        cache_control={"type": "ephemeral"},  # auto-caches the growing message prefix inside the agent loop
        tools=tools,
        # WHY auto: forced tool_choice ('any'/'tool') returns 400 on Sonnet 5.5. The prompt requires `respond`
        # and agent.py nudges if the model answers in plain text; strict schemas keep arguments valid.
        tool_choice={"type": "auto"},
        messages=messages,
        output_config={"effort": config.llm_effort},
    )
    if model == config.primary_model and config.llm_server_fallback:
        # Server-side refusal fallback (beta): if a safety classifier declines, the API reroutes by category.
        return client().beta.messages.create(**kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    return client().messages.create(**kwargs)


def usage_of(response) -> dict:
    u = response.usage
    return {
        "input": u.input_tokens or 0,
        "output": u.output_tokens or 0,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }
