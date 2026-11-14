"""The tool-calling loop. One call to run_agent() = one customer turn (spec §4 request lifecycle)."""
import json
import time
from dataclasses import dataclass, field

from src.const.config import config
from src.helper.dates import as_of
from src.helper.prompt_loader import load_prompt
from src.helper.text import normalise
from src.modules.agent import safe_responses
from src.modules.agent.tools_schema import TOOLS
from src.modules.cache import lru
from src.modules.decision.facts import compute_facts
from src.modules.decision.limit_increase_rules import evaluate
from src.modules.guardrails import citation_validator, input_guard, output_guard
from src.modules.llm import llm_router
from src.modules.retrieval import retriever
from src.modules.telemetry.logger import log
from src.modules.telemetry.tracer import Trace
from src.modules.tools.errors import CustomerNotFound
from src.modules.tools.tool_runner import ACCOUNT_STATE_UNAVAILABLE, run_get_account_state, run_raise_service_request

NUDGE = "You must finish by calling the respond tool with the structured answer. Do not answer in plain text."


@dataclass
class TurnContext:
    """Everything that happened in this turn — the citation validator checks claims against it."""
    customer_id: str  # bound from the request context; the model never supplies it
    tenant_id: str
    conversation_id: str | None
    trace: Trace
    raw_account: dict | None = None      # unredacted, server-side only
    account_state: dict | None = None    # redacted copy the model saw
    account_attempted: bool = False
    account_unavailable: bool = False
    facts: dict | None = None
    eligibility: dict | None = None
    retrieved: dict = field(default_factory=dict)       # (doc_id, section) -> chunk
    misses: list = field(default_factory=list)          # queries that returned grounded=false
    conflicts: dict = field(default_factory=dict)       # conflict id -> note
    service_requests: dict = field(default_factory=dict)  # request_id -> tool result
    invalid_category_count: int = 0


def _wrap(payload: dict) -> str:
    return f"<tool_output>\n{json.dumps(payload, default=str)}\n</tool_output>"


def _run_tool(name: str, args: dict, turn: TurnContext, emit) -> str:
    if name == "search_policy":
        emit("stage", {"stage": "retrieving"})
        query = str(args.get("query", ""))[:300]
        with turn.trace.span("retrieval.hybrid", query=query) as attrs:
            result = retriever.search(query, turn.tenant_id)
            top = result["chunks"][0] if result["chunks"] else None
            attrs.update(grounded=result["grounded"], top_vec_sim=result["top_vec_sim"],
                         top=f"{top['doc_id']}§{top['section']}" if top else None,
                         rrf=top["scores"]["rrf"] if top else None, cache_hit=result["cache_hit"],
                         conflicts=[c["id"] for c in result["conflicts"]])
        for ch in result["chunks"]:
            turn.retrieved[(ch["doc_id"], ch["section"])] = ch
        for note in result["conflicts"]:
            turn.conflicts[note["id"]] = note
        if not result["grounded"]:
            turn.misses.append(query)
            turn.trace.flag("retrieval_miss")
        return retriever.format_for_llm(result)

    emit("stage", {"stage": "tool_call", "tool": name})
    if name == "get_account_state":
        turn.account_attempted = True
        state = run_get_account_state(turn)
        if state is ACCOUNT_STATE_UNAVAILABLE:
            return _wrap(state)
        turn.facts = turn.facts or compute_facts(turn.raw_account)
        return _wrap({"account": state, "derived_facts": turn.facts})

    if name == "check_limit_increase_eligibility":
        turn.account_attempted = True
        state = run_get_account_state(turn)
        if state is ACCOUNT_STATE_UNAVAILABLE:
            return _wrap(state)
        with turn.trace.span("decision.eligibility") as attrs:
            turn.facts = turn.facts or compute_facts(turn.raw_account)
            turn.eligibility = evaluate(turn.raw_account, turn.facts)
            attrs.update(outcome=turn.eligibility["outcome"], reason_code=turn.eligibility["reason_code"],
                         rules=[r["rule"] for r in turn.eligibility["rules"]])
        return _wrap({"eligibility": turn.eligibility, "derived_facts": turn.facts})

    if name == "raise_service_request":
        return _wrap(run_raise_service_request(turn, str(args.get("category", "")), str(args.get("summary", ""))))

    return _wrap({"error": f"unknown tool {name}"})


def _loop(turn: TurnContext, system: str, messages: list, emit) -> dict:
    deadline = time.monotonic() + config.agent_timeout_s
    repaired = False
    for _ in range(config.agent_max_iterations):
        if time.monotonic() > deadline:
            turn.trace.flag("agent_timeout")
            return safe_responses.validation_failed(turn)
        response = llm_router.call(system, TOOLS, messages, turn.trace)
        messages.append({"role": "assistant", "content": response.content})  # unchanged (keeps thinking blocks valid)
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            messages.append({"role": "user", "content": NUDGE})
            continue

        results, respond_block = [], None
        for block in tool_uses:
            if block.name == "respond":
                respond_block = block
                continue
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": _run_tool(block.name, block.input or {}, turn, emit)})

        if respond_block is not None:
            emit("stage", {"stage": "validating"})
            with turn.trace.span("validate.citations") as attrs:
                payload, errors = citation_validator.validate(respond_block.input, turn)
                attrs.update(ok=not errors, errors=errors[:10])
            if payload is not None and not errors:
                with turn.trace.span("guard.output") as attrs:
                    errors = output_guard.check(payload, turn)
                    attrs.update(ok=not errors, violations=errors)
            if not errors:
                return payload.model_dump()
            if repaired:
                turn.trace.flag("citation_validation_failed")
                log("validation_failed", level="warning", trace_id=turn.trace.trace_id, errors=errors[:5])
                return safe_responses.validation_failed(turn)
            repaired = True
            turn.trace.validation_retries += 1
            turn.trace.flag("citation_repair")
            results.append({"type": "tool_result", "tool_use_id": respond_block.id, "is_error": True,
                            "content": "Your response failed validation. Fix every error and call respond again:\n- "
                                       + "\n- ".join(errors)})
        messages.append({"role": "user", "content": results})
    turn.trace.flag("max_iterations")
    return safe_responses.validation_failed(turn)


def _static(turn: TurnContext, message: str) -> dict:
    try:
        retrieval = retriever.search(message, turn.tenant_id)
        for ch in retrieval["chunks"]:
            turn.retrieved[(ch["doc_id"], ch["section"])] = ch
    except Exception:
        retrieval = None
    turn.trace.flag("static_mode")
    return safe_responses.static_mode(turn, retrieval)


def _finalize(payload: dict, turn: TurnContext) -> dict:
    """Deterministic post-processing: routing/SLA from the tool result, conflicts from retrieval, message from claims."""
    sr = None
    if payload.get("routing") and payload["routing"].get("request_id") in turn.service_requests:
        sr = turn.service_requests[payload["routing"]["request_id"]]
    elif turn.service_requests:
        sr = list(turn.service_requests.values())[-1]
    if sr:
        payload["routing"] = {"queue": sr["queue"], "request_id": sr["request_id"], "sla": sr["sla"]}
    cited = {c["doc_id"] for cl in payload["claims"] for c in cl["citations"] if c["kind"] == "policy"}
    payload["conflicts_noted"] = [{"docs": n["docs"], "prevails": n["prevails"]}
                                  for n in turn.conflicts.values() if n["prevails"] in cited]
    payload["customer_message"] = " ".join(c["text"].strip() for c in payload["claims"])
    flat = []
    for cl in payload["claims"]:
        for c in cl["citations"]:
            if c not in flat:
                flat.append(c)
    payload["citations_flat"] = flat
    return payload


def _history(conversation_id: str | None) -> list[dict]:
    if not conversation_id:
        return []
    try:
        from src.db.repositories import conversation_repo

        msgs = []
        for m in conversation_repo.recent_messages(conversation_id, limit=4):
            text = m["content"].get("text") if m["role"] == "user" else m["content"].get("customer_message")
            if text:
                msgs.append({"role": m["role"], "content": f"(earlier in this conversation) {text}"})
        while msgs and msgs[0]["role"] != "user":  # must start with a user turn
            msgs.pop(0)
        return msgs
    except Exception:
        return []


def _persist_conversation(turn: TurnContext, conversation_id: str | None, message: str) -> str | None:
    try:
        from src.db.repositories import conversation_repo

        cid = conversation_repo.get_or_create(conversation_id, turn.tenant_id, turn.trace.customer_id_hash)
        conversation_repo.add_message(cid, "user", {"text": message})
        return cid
    except Exception:
        return None  # DB down: answer anyway, just without conversation memory


def run_agent(customer_id: str, message: str, tenant_id: str = "default",
              conversation_id: str | None = None, on_event=None) -> dict:
    """Raises CustomerNotFound (-> HTTP 404). Every other outcome is a structured response."""
    emit = on_event or (lambda *_: None)
    prompt = load_prompt()
    trace = Trace(tenant_id, customer_id, prompt, conversation_id)
    turn = TurnContext(customer_id, tenant_id, None, trace)
    flags = input_guard.check(message, customer_id)["flags"]
    for f in flags:
        trace.flag(f)

    history = _history(conversation_id)
    turn.conversation_id = _persist_conversation(turn, conversation_id, message)
    trace.conversation_id = turn.conversation_id

    # Answer cache: only for account-independent answers to a fresh question (see _cacheable).
    cache_key = (normalise(message), tenant_id, prompt["version"])
    cached = lru.get("answer", cache_key) if config.answer_cache_ttl_s > 0 and not history and not flags else None

    try:
        with trace.span("request"):
            if cached is not None:
                trace.cache_hit = True
                payload = json.loads(json.dumps(cached))
            else:
                user_content = (f"<context>as_of_date: {as_of().isoformat()}\ninput_flags: {flags or 'none'}</context>\n"
                                f"<customer_message>\n{message}\n</customer_message>")
                messages = history + [{"role": "user", "content": user_content}]
                try:
                    payload = _loop(turn, prompt["content"], messages, emit)
                except llm_router.LLMUnavailable:
                    payload = _static(turn, message)
    except CustomerNotFound:
        trace.end("refused", error_code="CUSTOMER_NOT_FOUND")
        raise

    if cached is None and _cacheable(payload, turn, flags, history):
        lru.put("answer", cache_key, payload)
    final = _finalize(payload, turn)
    row = trace.end(final["status"])
    final.update(trace_id=trace.trace_id, conversation_id=turn.conversation_id, meta={
        "prompt_version": prompt["version"], "prompt_sha": prompt["sha256"][:12], "model": row["model_used"],
        "fallback_used": row["fallback_used"], "latency_ms": row["latency_ms"], "cache_hit": row["cache_hit"],
        "tokens": {"input": row["input_tokens"], "output": row["output_tokens"], "cache_read": row["cache_read_tokens"]},
        "cost_usd": row["cost_usd"], "llm_calls": row["llm_calls"], "flags": row["flags"]})
    if turn.conversation_id:
        try:
            from src.db.repositories import conversation_repo

            conversation_repo.add_message(turn.conversation_id, "assistant",
                                          {"customer_message": final["customer_message"], "status": final["status"],
                                           "trace_id": trace.trace_id})
        except Exception:
            pass
    return final


def _cacheable(payload: dict, turn: TurnContext, flags: list, history: list) -> bool:
    # WHY so strict: a cached answer is served to *other customers*. Anything that touched account state,
    # raised a request, or came from a degraded path must never be reused.
    return (payload["status"] in ("answered", "answered_with_gap") and not turn.account_attempted
            and not turn.service_requests and not flags and not history and not turn.trace.flags)
