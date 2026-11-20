"""Fallback chain: Google Gemini (primary, one step per model in GEMINI_MODEL)
             -> xAI Grok (fallback, one step per model in GROK_MODEL)
             -> LLMUnavailable, which the API returns as 503 LLM_LIMIT_REACHED ("try again tomorrow").

When do we move on to the next step?
  * timeout (hung call)            -> next model immediately (no retry)
  * 429 with a short Retry-After    -> wait that long (<= LLM_RATE_LIMIT_MAX_WAIT_MS) and retry the same model once
                                       (per-minute token limits, e.g. Groq free tier: 8k tokens/min)
  * 429 quota exhausted / long wait -> next model immediately
  * 5xx / network error            -> one retry with backoff, then next model
  * 4xx bad request / refusal      -> next model
  * bad key / no access / credit   -> next model AND that model's breaker opens for 10 minutes
One circuit breaker per model, so a model that keeps failing is skipped without a network call.
"""
import time

from src.const.config import config
from src.helper.async_utils import CircuitBreaker, backoff_delay, p_limit, sleep
from src.modules.llm import gemini_client, grok_client
from src.modules.llm.errors import ProviderError, ProviderRateLimited, ProviderRetryable, ProviderTimeout
from src.modules.llm.pricing import cost_usd
from src.modules.telemetry.logger import log


class LLMUnavailable(Exception):
    """Every model failed — the API returns 503 LLM_LIMIT_REACHED."""


class SimulatedOutage(ProviderRetryable):
    """LLM_FORCE_PRIMARY_FAIL=1: simulates a Google outage so the demo/eval can prove the Grok fallback."""


class ModelRefusal(Exception):
    pass


PRIMARY_PROVIDER = "gemini"
# "groq" = the same OpenAI-compatible client pointed at Groq (GROK_BASE_URL=https://api.groq.com/openai/v1).
_PROVIDERS = {"gemini": gemini_client, "grok": grok_client, "groq": grok_client}
PERMANENT_OPEN_S = 600
_breakers: dict[str, CircuitBreaker] = {}
_limiter = p_limit(config.llm_concurrency)


def breaker(model: str) -> CircuitBreaker:
    if model not in _breakers:
        _breakers[model] = CircuitBreaker(f"llm:{model}", config.circuit_fails_to_open, config.circuit_open_s)
    return _breakers[model]


def chain() -> list[tuple[str, str]]:
    """(model, provider) in order of preference. A provider without an API key is skipped."""
    steps = []
    if config.gemini_api_key:
        steps += [(m, "gemini") for m in config.gemini_models]
    if config.grok_api_key:
        steps += [(m, grok_client.provider_name()) for m in config.grok_models]
    return steps


def usage_of(response) -> dict:
    u = response.usage
    return {
        "input": getattr(u, "input_tokens", 0) or 0,
        "output": getattr(u, "output_tokens", 0) or 0,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


def call(system: str, tools: list[dict], messages: list, trace, deadline: float | None = None):
    """deadline = time.monotonic() value the agent must answer by. Retry-After waits never run past it, and no new
    model is tried after it: the caller returns the 503 in time instead of the HTTP request timing out."""
    if config.llm_disabled:
        raise LLMUnavailable("LLM disabled by config")
    steps = chain()
    if not steps:
        trace.flag("no_llm_configured")
        log("llm_not_configured", level="warning", hint="set GEMINI_API_KEY and/or GROK_API_KEY in .env")
    for model, provider in steps:
        if deadline is not None and time.monotonic() > deadline - 1.0:
            trace.flag("llm_budget_exhausted")
            break
        is_primary = provider == PRIMARY_PROVIDER
        if not breaker(model).allow():
            trace.flag(f"circuit_open:{model}")
            continue
        attempts = 1 + config.llm_max_retries
        for attempt in range(1, attempts + 1):
            try:
                with trace.span("llm.call", model=model, provider=provider, attempt=attempt,
                                iteration=trace.llm_calls + 1) as attrs:
                    if is_primary and config.llm_force_primary_fail:
                        raise SimulatedOutage("forced primary (Google) failure")
                    with _limiter:
                        response = _PROVIDERS[provider].call(model, system, tools, messages)
                    if response.stop_reason == "refusal":
                        raise ModelRefusal(f"{provider} safety stop")
                    u = usage_of(response)
                    served_by = getattr(response, "model", None) or model
                    cost = cost_usd(model, u["input"], u["output"], u["cache_read"], u["cache_write"])
                    attrs.update(served_by=served_by, input_tokens=u["input"], output_tokens=u["output"],
                                 cache_read_tokens=u["cache_read"], stop_reason=response.stop_reason,
                                 cost_usd=round(cost, 6), tool_calls=[b.name for b in response.content if b.type == "tool_use"])
                trace.add_usage(served_by, u["input"], u["output"], u["cache_read"], cost, provider=provider)
                breaker(model).success()
                if not is_primary:
                    trace.fallback_used = True
                    trace.flag("fallback_used")
                log("llm_call_ok", provider=provider, model=model, attempt=attempt, input_tokens=u["input"],
                    output_tokens=u["output"], trace_id=trace.trace_id)
                return response
            except ProviderRateLimited as e:
                wait = e.retry_after
                fits = deadline is None or time.monotonic() + (wait or 0) < deadline - 3.0  # leave time to answer
                will_wait = wait is not None and wait <= config.llm_rate_limit_max_wait_s and attempt < attempts and fits
                log("llm_call_failed", level="warning", provider=provider, model=model, attempt=attempt,
                    error="ProviderRateLimited", retry_after_s=wait, action=f"wait {wait:.1f}s and retry" if will_wait else "next_model",
                    detail=str(e)[:160], trace_id=trace.trace_id)
                if not will_wait:
                    break
                trace.flag("rate_limit_wait")
                sleep(wait + 0.25)
            except (ProviderTimeout, SimulatedOutage) as e:
                log("llm_call_failed", level="warning", provider=provider, model=model, attempt=attempt,
                    error=type(e).__name__, detail=str(e)[:160], action="next_model", trace_id=trace.trace_id)
                break
            except ProviderRetryable as e:
                log("llm_call_failed", level="warning", provider=provider, model=model, attempt=attempt,
                    error=type(e).__name__, detail=str(e)[:160], action="retry" if attempt < attempts else "next_model",
                    trace_id=trace.trace_id)
                if attempt < attempts:
                    sleep(backoff_delay(attempt, base_s=0.5))
            except Exception as e:  # 4xx, refusal, adapter bug: retrying the same model will not help
                permanent = isinstance(e, ProviderError) and e.permanent
                log("llm_call_failed", level="error" if permanent else "warning", provider=provider, model=model,
                    attempt=attempt, error=type(e).__name__, detail=str(e)[:200], permanent=permanent,
                    action="next_model", trace_id=trace.trace_id)
                if permanent:
                    # WHY: an invalid key or exhausted credit will not fix itself in 30 s. Opening the breaker for
                    # 10 minutes stops every request paying ~1 s for a doomed call.
                    breaker(model).open_for(PERMANENT_OPEN_S)
                break
        breaker(model).failure()
    trace.flag("llm_unavailable")
    log("llm_unavailable", level="error", tried=[m for m, _ in steps], trace_id=trace.trace_id)
    raise LLMUnavailable("all models failed")
