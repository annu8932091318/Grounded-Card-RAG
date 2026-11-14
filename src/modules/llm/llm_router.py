"""Primary -> fallback -> (caller switches to) static mode. Circuit breaker on the primary.
A second provider would be one more entry in _chain(); it is a declared omission."""
import anthropic

from src.const.config import config
from src.helper.async_utils import CircuitBreaker, backoff_delay, p_limit, sleep
from src.modules.llm import anthropic_client
from src.modules.llm.pricing import cost_usd
from src.modules.telemetry.logger import log


class LLMUnavailable(Exception):
    """Every model failed — agent.py answers in static (no-LLM) mode."""


class SimulatedOutage(Exception):
    """LLM_FORCE_PRIMARY_FAIL=1: lets the demo/eval prove the fallback path without breaking a real key."""


class ModelRefusal(Exception):
    pass


RETRYABLE = (
    anthropic.RateLimitError,       # 429
    anthropic.InternalServerError,  # 5xx (incl. overloaded)
    anthropic.APIConnectionError,   # network + APITimeoutError
    SimulatedOutage,
)

primary_breaker = CircuitBreaker("llm_primary", config.circuit_fails_to_open, config.circuit_open_s)
_limiter = p_limit(config.llm_concurrency)


def _chain() -> list[tuple[str, bool]]:
    return [(config.primary_model, True), (config.fallback_model, False)]


def call(system: str, tools: list[dict], messages: list, trace):
    if config.llm_disabled:
        raise LLMUnavailable("LLM disabled by config")
    for model, is_primary in _chain():
        if is_primary and not primary_breaker.allow():
            trace.flag("primary_circuit_open")
            continue
        attempts = 1 + config.llm_max_retries
        for attempt in range(1, attempts + 1):
            try:
                with trace.span("llm.call", model=model, attempt=attempt, iteration=trace.llm_calls + 1) as attrs:
                    if is_primary and config.llm_force_primary_fail:
                        raise SimulatedOutage("forced primary failure")
                    with _limiter:
                        response = anthropic_client.call(model, system, tools, messages)
                    if response.stop_reason == "refusal":
                        raise ModelRefusal(str(getattr(response, "stop_details", "")))
                    u = anthropic_client.usage_of(response)
                    served_by = getattr(response, "model", None) or model
                    cost = cost_usd(served_by, u["input"], u["output"], u["cache_read"], u["cache_write"])
                    attrs.update(served_by=served_by, input_tokens=u["input"], output_tokens=u["output"],
                                 cache_read_tokens=u["cache_read"], cache_write_tokens=u["cache_write"],
                                 stop_reason=response.stop_reason, cost_usd=round(cost, 6),
                                 tool_calls=[b.name for b in response.content if b.type == "tool_use"])
                trace.add_usage(served_by, u["input"], u["output"], u["cache_read"], cost)
                if is_primary:
                    primary_breaker.success()
                else:
                    trace.fallback_used = True
                    trace.flag("fallback_used")
                return response
            except RETRYABLE as e:
                log("llm_call_failed", level="warning", model=model, attempt=attempt, error=type(e).__name__,
                    trace_id=trace.trace_id)
                if attempt < attempts and not isinstance(e, SimulatedOutage):
                    sleep(backoff_delay(attempt, base_s=0.5))
            except Exception as e:  # 4xx, refusal, bad response: retrying the same model will not help
                log("llm_call_failed", level="warning", model=model, attempt=attempt, error=type(e).__name__,
                    trace_id=trace.trace_id)
                break
        if is_primary:
            primary_breaker.failure()
    trace.flag("llm_unavailable")
    raise LLMUnavailable("all models failed")
