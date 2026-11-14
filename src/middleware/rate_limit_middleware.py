"""Per-api-key sliding-window limiter (in-process; Redis at multi-node scale)."""
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, Request

from src.const.config import config
from src.const.errors import AppError
from src.middleware.auth_middleware import require_api_key

_windows: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def rate_limit(request: Request, ctx: dict = Depends(require_api_key)) -> dict:
    now = time.monotonic()
    with _lock:
        window = _windows[ctx["api_key"]]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= config.rate_limit_per_min:
            raise AppError("RATE_LIMITED")
        window.append(now)
    return ctx
