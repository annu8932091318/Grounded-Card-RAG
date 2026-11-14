"""In-memory counters + latency histogram for GET /metrics (single node; see README for the Prometheus/OTel path)."""
import threading
from collections import Counter, deque

_lock = threading.Lock()
_status = Counter()
_counters = Counter()
_latencies: deque = deque(maxlen=2000)


def record(trace_row: dict, spans: list[dict]) -> None:
    with _lock:
        _status[trace_row["status"] or "unknown"] += 1
        _latencies.append(trace_row["latency_ms"] or 0)
        _counters["requests"] += 1
        _counters["input_tokens"] += trace_row["input_tokens"]
        _counters["output_tokens"] += trace_row["output_tokens"]
        _counters["cache_read_tokens"] += trace_row["cache_read_tokens"]
        _counters["cost_micro_usd"] += int(trace_row["cost_usd"] * 1_000_000)
        _counters["fallback"] += int(trace_row["fallback_used"])
        _counters["validation_repair"] += int("citation_repair" in trace_row["flags"])
        _counters["answer_cache_hit"] += int(trace_row["cache_hit"])
        for s in spans:
            if s["name"].startswith("tool."):
                _counters["tool_calls"] += 1
                _counters["tool_failures"] += int(s["error"] is not None or bool(s["attrs"].get("failed")))


def _pct(values: list[int], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))]


def snapshot() -> dict:
    from src.modules.cache.lru import stats as cache_stats  # WHY local: avoid import cycle

    with _lock:
        n = _counters["requests"] or 1
        lat = list(_latencies)
        return {
            "requests_total": _counters["requests"],
            "requests_by_status": dict(_status),
            "latency_ms": {"p50": _pct(lat, 0.50), "p95": _pct(lat, 0.95)},
            "tokens": {"input": _counters["input_tokens"], "output": _counters["output_tokens"],
                       "cache_read": _counters["cache_read_tokens"]},
            "cost_usd_total": _counters["cost_micro_usd"] / 1_000_000,
            "cost_usd_per_request": round(_counters["cost_micro_usd"] / 1_000_000 / n, 6),
            "tool_failure_rate": round(_counters["tool_failures"] / (_counters["tool_calls"] or 1), 4),
            "fallback_rate": round(_counters["fallback"] / n, 4),
            "validation_repair_rate": round(_counters["validation_repair"] / n, 4),
            "answer_cache_hits": _counters["answer_cache_hit"],
            "caches": cache_stats(),
        }


def prometheus_text() -> str:
    s = snapshot()
    lines = [f"agent_requests_total {s['requests_total']}"]
    lines += [f'agent_requests_by_status{{status="{k}"}} {v}' for k, v in s["requests_by_status"].items()]
    lines += [
        f"agent_latency_p50_ms {s['latency_ms']['p50']}",
        f"agent_latency_p95_ms {s['latency_ms']['p95']}",
        f"agent_tokens_input_total {s['tokens']['input']}",
        f"agent_tokens_output_total {s['tokens']['output']}",
        f"agent_cost_usd_total {s['cost_usd_total']}",
        f"agent_tool_failure_rate {s['tool_failure_rate']}",
        f"agent_fallback_rate {s['fallback_rate']}",
        f"agent_validation_repair_rate {s['validation_repair_rate']}",
    ]
    lines += [f'agent_cache_hit_rate{{cache="{k}"}} {v["hit_rate"]}' for k, v in s["caches"].items()]
    return "\n".join(lines) + "\n"
