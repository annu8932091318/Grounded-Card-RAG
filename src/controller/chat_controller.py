"""Calls the agent and shapes the HTTP response. All agent outcomes (declined, refused, degraded) are 200:
they are designed outcomes, not errors. Errors: unknown customer -> 404; every LLM down / out of quota -> 503
LLM_LIMIT_REACHED ("try again tomorrow")."""
import contextvars
import json
import queue
import threading

from fastapi.responses import StreamingResponse

from src.const.errors import ERRORS, AppError
from src.middleware.validate_middleware import ChatRequest
from src.modules.agent.agent import LLMLimitReached, run_agent
from src.modules.telemetry.logger import log
from src.modules.tools.errors import CustomerNotFound

_RESPONSE_KEYS = ["trace_id", "conversation_id", "status", "customer_message", "claims", "decision", "routing",
                  "unanswered", "conflicts_noted", "citations_flat", "meta"]


def _shape(result: dict) -> dict:
    return {k: result.get(k) for k in _RESPONSE_KEYS}


def _remember(state, result: dict) -> None:
    """request_context_middleware writes this into the call's req_res_log row: provider/model, every LLM attempt,
    tokens, cost, flags, spans, and (for streams) the final answer, which an Idempotency-Key replay returns."""
    internal = result.pop("_internal", None) or {}
    state.agent = {**internal, "response_body": _shape(result)}


def chat(body: ChatRequest, ctx: dict, state=None) -> dict:
    try:
        result = run_agent(body.customer_id, body.message, ctx["tenant_id"], body.conversation_id)
    except CustomerNotFound:
        raise AppError("CUSTOMER_NOT_FOUND") from None
    except LLMLimitReached as e:
        if state is not None:
            state.agent = dict(e.internal)  # req_res_log still records every failed model attempt
        raise AppError("LLM_LIMIT_REACHED") from None
    if state is not None:
        _remember(state, result)
    return _shape(result)


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def chat_stream(body: ChatRequest, ctx: dict, state=None) -> StreamingResponse:
    """Buffer-then-release: stage events stream live; claims are emitted only after validation passed,
    because enforced citations need the complete structured answer before anything reaches the customer."""
    events: queue.Queue = queue.Queue()

    def worker():
        try:
            result = run_agent(body.customer_id, body.message, ctx["tenant_id"], body.conversation_id,
                               on_event=lambda ev, data: events.put((ev, data)))
            if state is not None:
                _remember(state, result)
            events.put(("done", _shape(result)))
        except CustomerNotFound:
            events.put(("error", {"code": "CUSTOMER_NOT_FOUND"}))
        except LLMLimitReached as e:
            if state is not None:
                state.agent = dict(e.internal)
            events.put(("error", {"code": "LLM_LIMIT_REACHED", "message": ERRORS["LLM_LIMIT_REACHED"][1]}))
        except Exception:
            log("chat_stream_failed", level="error", exc_info=True)
            events.put(("error", {"code": "INTERNAL"}))

    run_ctx = contextvars.copy_context()  # keeps the requestId in the worker thread's log lines
    threading.Thread(target=run_ctx.run, args=(worker,), daemon=True).start()

    def generate():
        while True:
            ev, data = events.get()
            if ev == "done":
                for claim in data["claims"]:
                    yield _sse("claim", {"id": claim["id"], "type": claim["type"], "text": claim["text"],
                                         "citations": claim["citations"]})
                yield _sse("final", data)
                return
            yield _sse(ev, data)
            if ev == "error":
                return

    return StreamingResponse(generate(), media_type="text/event-stream")
