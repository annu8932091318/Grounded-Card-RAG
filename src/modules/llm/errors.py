"""Provider-neutral LLM errors. Both adapters (gemini_client, grok_client) raise only these, so the router
decides retry / fail-over / circuit-breaker behaviour in one place, the same way for every provider."""
import re


class ProviderError(Exception):
    """Non-retryable (4xx). permanent=True means the account itself is broken (bad key, no access, no credit):
    the router opens that model's breaker for 10 minutes instead of paying for a doomed call on every request."""

    def __init__(self, message: str, status: int | None = None, permanent: bool = False):
        super().__init__(message)
        self.status = status
        self.permanent = permanent


class ProviderRetryable(Exception):
    """5xx / network: one retry on the same model is worth it."""


class ProviderTimeout(ProviderRetryable):
    """Hung call. Measured on the Gemini free tier: ~1 in 4 calls hang while healthy calls finish < 5 s,
    so a hung call moves straight to the next model instead of retrying the same one."""


class ProviderRateLimited(ProviderRetryable):
    """429. retry_after = seconds the provider asked us to wait, if it said.
    Short wait (per-minute token limit, e.g. Groq free tier 8k tokens/min) -> the router waits and retries.
    Long or unknown wait (daily quota exhausted) -> next model / provider at once."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def retry_after_seconds(headers, body: str) -> float | None:
    """Retry-After header, else the provider's text: Groq 'try again in 7.66s', Gemini '"retryDelay": "41s"'."""
    value = (headers or {}).get("retry-after")
    if value:
        try:
            return float(value)
        except ValueError:
            pass
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", body) or re.search(r'"retryDelay":\s*"([\d.]+)s"', body)
    if not m:
        return None
    if m.re.pattern.startswith("try"):
        return int(m.group(1) or 0) * 60 + float(m.group(2))
    return float(m.group(1))


def from_status(provider: str, status: int, body: str, headers=None) -> Exception:
    """Map an HTTP status to the right error class."""
    detail = f"{provider} HTTP {status}: {body[:200]}"
    if status == 429:
        return ProviderRateLimited(detail, retry_after_seconds(headers, body))
    if status >= 500:
        return ProviderRetryable(detail)
    lowered = body.lower()
    permanent = status in (401, 403) or "api key" in lowered or "credit" in lowered or "billing" in lowered
    return ProviderError(detail, status=status, permanent=permanent)
