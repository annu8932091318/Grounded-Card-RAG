"""with_timeout, retry (backoff + jitter), circuit breaker, concurrency limit.

WHY sync + threads instead of asyncio: the agent loop is sequential and FastAPI runs sync
endpoints in a threadpool. Plain functions are easier to explain line by line.
"""
import contextvars
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

_executor = ThreadPoolExecutor(max_workers=32, thread_name_prefix="tool")


class TimeoutExceeded(Exception):
    pass


def with_timeout(fn, timeout_s: float, *args, **kwargs):
    """Run fn in a worker thread; raise TimeoutExceeded if it does not finish in time.
    Limitation: Python cannot kill the thread — it finishes in the background and its result is dropped."""
    # copy_context: the worker thread keeps the caller's requestId, so tool logs stay joinable to the request.
    future = _executor.submit(contextvars.copy_context().run, fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout_s)
    except FutureTimeout as e:
        raise TimeoutExceeded(f"timed out after {timeout_s}s") from e


def backoff_delay(attempt: int, base_s: float = 0.2) -> float:
    """attempt 1 -> ~200ms, attempt 2 -> ~400ms, +/-25% jitter."""
    delay = base_s * (2 ** (attempt - 1))
    return delay * random.uniform(0.75, 1.25)


def sleep(seconds: float) -> None:
    time.sleep(seconds)


class CircuitBreaker:
    """Opens after N consecutive failures; fails fast while open; half-opens after open_s."""

    def __init__(self, name: str, fails_to_open: int, open_s: float):
        self.name, self.fails_to_open, self.open_s = name, fails_to_open, open_s
        self.failures = 0
        self.opened_at: float | None = None
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self.opened_at is None:
                return True
            if time.monotonic() - self.opened_at >= self.open_s:
                self.opened_at = None  # half-open: let one call through
                self.failures = self.fails_to_open - 1
                return True
            return False

    def success(self) -> None:
        with self._lock:
            self.failures, self.opened_at = 0, None

    def failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.fails_to_open:
                # max(): never shorten a longer open window set by open_for()
                self.opened_at = max(self.opened_at or 0.0, time.monotonic())

    def open_for(self, seconds: float) -> None:
        """Force the breaker open for `seconds` (used for permanent failures such as exhausted credit)."""
        with self._lock:
            self.failures = self.fails_to_open
            self.opened_at = time.monotonic() + max(0.0, seconds - self.open_s)

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None


def p_limit(n: int) -> threading.BoundedSemaphore:
    """Concurrency limiter: `with limiter:` around the call."""
    return threading.BoundedSemaphore(n)
