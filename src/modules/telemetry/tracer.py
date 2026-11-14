"""One Trace per agent turn: spans, flags, token usage, cost, every LLM attempt. At the end it becomes one
req_res_log row: written by the HTTP middleware for API calls, or directly here for scripts (eval / demo)."""
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from src.helper.hash import customer_hash
from src.modules.telemetry import metrics
from src.modules.telemetry.logger import in_http_var, log, request_id_var, script_source_var


class Trace:
    def __init__(self, tenant_id: str, customer_id: str, prompt: dict, conversation_id: str | None = None):
        self.trace_id = str(uuid.uuid4())
        self.tenant_id = tenant_id
        self.customer_id_hash = customer_hash(customer_id)
        self.conversation_id = conversation_id
        self.prompt_version, self.prompt_sha = prompt["version"], prompt["sha256"]
        self.started_at = datetime.now(timezone.utc)
        self._t0 = time.perf_counter()
        self.spans: list[dict] = []
        self._stack: list[str] = []  # open span ids -> parent_id of the next span
        self.flags: set[str] = set()
        self.model_used: str | None = None
        self.provider: str | None = None
        rid = request_id_var.get()
        self.request_id: str | None = None if rid == "-" else rid  # = req_res_log.idempotency_key + log file requestId
        self.fallback_used = False
        self.llm_calls = self.input_tokens = self.output_tokens = self.cache_read_tokens = 0
        self.cost_usd = 0.0
        self.validation_retries = 0
        self.cache_hit = False
        self.status: str | None = None
        self.error_code: str | None = None
        self.latency_ms: int | None = None

    @contextmanager
    def span(self, name: str, **attrs):
        """with trace.span('tool.get_account_state', attempt=1) as attrs: attrs['x'] = ..."""
        s = {
            "span_id": str(uuid.uuid4()),
            "parent_id": self._stack[-1] if self._stack else None,
            "name": name,
            "started_at": datetime.now(timezone.utc),
            "attrs": dict(attrs),
            "error": None,
            "duration_ms": None,
        }
        self.spans.append(s)
        self._stack.append(s["span_id"])
        t0 = time.perf_counter()
        try:
            yield s["attrs"]
        except Exception as e:
            s["error"] = type(e).__name__
            raise
        finally:
            s["duration_ms"] = int((time.perf_counter() - t0) * 1000)
            self._stack.pop()

    def flag(self, name: str) -> None:
        self.flags.add(name)

    def add_usage(self, model: str, input_tokens: int, output_tokens: int, cache_read: int, cost: float,
                  provider: str | None = None) -> None:
        self.llm_calls += 1
        self.model_used = model
        self.provider = provider
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read
        self.cost_usd += cost

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)

    def to_row(self) -> dict:
        return {
            "trace_id": self.trace_id, "conversation_id": self.conversation_id, "tenant_id": self.tenant_id,
            "customer_id_hash": self.customer_id_hash, "started_at": self.started_at,
            "latency_ms": self.latency_ms, "status": self.status, "prompt_version": self.prompt_version,
            "prompt_sha": self.prompt_sha, "model_used": self.model_used, "provider": self.provider,
            "request_id": self.request_id, "fallback_used": self.fallback_used,
            "llm_calls": self.llm_calls, "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens, "cost_usd": round(self.cost_usd, 6),
            "cache_hit": self.cache_hit, "validation_retries": self.validation_retries,
            "error_code": self.error_code, "flags": sorted(self.flags),
        }

    def llm_attempts(self) -> list[dict]:
        """Every model call in this turn, failed or not: the raw material for 'which model fails / misbehaves'."""
        return [{"provider": s["attrs"].get("provider"), "model": s["attrs"].get("model"),
                 "attempt": s["attrs"].get("attempt"), "ok": s["error"] is None, "error": s["error"],
                 "duration_ms": s["duration_ms"], "input_tokens": s["attrs"].get("input_tokens", 0),
                 "output_tokens": s["attrs"].get("output_tokens", 0)}
                for s in self.spans if s["name"] == "llm.call"]

    def log_fields(self, response: dict | None = None, message: str | None = None) -> dict:
        """The agent's part of a req_res_log row."""
        attempts = self.llm_attempts()
        failed = [a for a in attempts if not a["ok"]]
        return {
            "trace_id": self.trace_id, "customer_id_hash": self.customer_id_hash, "conversation_id": self.conversation_id,
            "agent_status": self.status, "error_code": self.error_code, "provider": self.provider,
            "model_used": self.model_used, "fallback_used": self.fallback_used, "cache_hit": self.cache_hit,
            "llm_calls": self.llm_calls, "llm_failures": len(failed),
            "models_failed": sorted({a["model"] for a in failed if a["model"]}), "llm_attempts": attempts,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens, "cost_usd": round(self.cost_usd, 6),
            "prompt_version": self.prompt_version, "prompt_sha": self.prompt_sha, "flags": sorted(self.flags),
            "spans": self.spans, "response_body": response, "request_message": message,
        }

    def end(self, status: str, error_code: str | None = None, response: dict | None = None,
            message: str | None = None) -> dict:
        self.status, self.error_code = status, error_code
        self.latency_ms = self.elapsed_ms()
        row = self.to_row()
        metrics.record(row, self.spans)
        log("request_completed", **{k: row[k] for k in ("trace_id", "status", "latency_ms", "provider", "model_used",
                                                        "input_tokens", "output_tokens", "cost_usd", "flags",
                                                        "customer_id_hash")})
        if not in_http_var.get():
            # Script run (eval / demo): no HTTP middleware will write the row, so write it here. Best effort.
            self._persist_script_row(response, message)
        return row

    def _persist_script_row(self, response: dict | None, message: str | None) -> None:
        try:
            from src.db.repositories import req_res_log_repo  # WHY local import: unit tests run without a DB

            fields = self.log_fields(_public(response), message)
            fields.pop("request_message")
            req_res_log_repo.upsert({**fields, "idempotency_key": self.request_id or self.trace_id,
                                     "tenant_id": self.tenant_id, "method": "RUN", "path": "agent.run_agent",
                                     "source": script_source_var.get(), "request_body": {"message": message},
                                     "status_code": None, "response_time_ms": self.latency_ms,
                                     "state": "completed" if self.status != "error" else "failed"})
        except Exception as e:  # telemetry must never break the customer's answer
            log("req_res_log_write_failed", level="warning", trace_id=self.trace_id, error=type(e).__name__)


def _public(response: dict | None) -> dict | None:
    return {k: v for k, v in response.items() if not k.startswith("_")} if response else None
