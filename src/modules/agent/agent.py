"""The tool-calling loop. One call to run_agent() = one customer turn (spec §4 request lifecycle)."""
import json
import time
from dataclasses import dataclass, field
from types import SimpleNamespace

from src.const.config import config
from src.helper.dates import as_of
from src.helper.prompt_loader import load_prompt
from src.modules.agent import safe_responses
from src.modules.agent.respond_schema import RespondPayload
from src.modules.agent.tools_schema import TOOLS
from src.modules.cache import semantic_cache
from src.modules.decision.facts import compute_facts
from src.modules.decision.limit_increase_rules import evaluate
from src.modules.guardrails import citation_validator, input_guard, output_guard
from src.modules.llm import llm_router
from src.modules.retrieval import retriever
from src.modules.telemetry.logger import log, request_id_var, reset_request_id, set_request_id
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
    input_flags: list = field(default_factory=list)  # from input_guard: injection_suspected, cross_customer_request


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
        log("policy_search", grounded=result["grounded"], top_vec_sim=result["top_vec_sim"],
            hits=[f"{c['doc_id']}§{c['section']}" for c in result["chunks"]], cache_hit=result["cache_hit"],
            conflicts=[c["id"] for c in result["conflicts"]], trace_id=turn.trace.trace_id)
        if not result["grounded"]:
            turn.misses.append(query)
            turn.trace.flag("retrieval_miss")
        return retriever.format_for_llm(result)

    emit("stage", {"stage": "tool_call", "tool": name})
    log("tool_call", tool=name, trace_id=turn.trace.trace_id)
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
        result = run_raise_service_request(turn, str(args.get("category", "")), str(args.get("summary", "")))
        if "request_id" not in result:
            return _wrap(result)
        # Return the routing policy with the result, so the queue SLA the customer is told is retrieved + citable.
        routing_chunk = _routing_policy_chunk(turn)
        excerpt = retriever.format_for_llm({"grounded": True, "chunks": [routing_chunk], "conflicts": []}) if routing_chunk else ""
        return _wrap(result) + ("\n" + excerpt if excerpt else "")

    return _wrap({"error": f"unknown tool {name}"})


def _routing_policy_chunk(turn: TurnContext) -> dict | None:
    try:
        from src.db.repositories import policy_repo

        chunk = policy_repo.get_chunk("POL-SVC-006", "body", turn.tenant_id)
    except Exception:
        return None
    if chunk:
        turn.retrieved[("POL-SVC-006", "body")] = chunk
    return chunk


def _complete_routing(raw: dict, turn: TurnContext) -> dict:
    """Request bookkeeping is code, not model output (eval: the model invented ids / forgot to cite the request).
    routing is rebuilt from the real tool result; a request_id the model cites is only corrected when exactly one
    request exists; and if no claim mentions the request, a deterministic claim citing it is added."""
    if not isinstance(raw, dict) or not isinstance(raw.get("claims"), list):
        return raw
    if raw.get("status") in ("refused", "answered_with_gap") and turn.misses and not turn.service_requests:
        # Spec D3: a coverage gap is answered-with-gap or refused, *then routed to GENERAL* — enforced in code
        # (eval EV-010: the model gave an honest gap answer but routed nobody).
        try:
            run_raise_service_request(turn, "GENERAL", "Question not covered by policy: " + "; ".join(turn.misses)[:200])
        except CustomerNotFound:
            raise
        except Exception:
            pass
    if not turn.service_requests:
        return raw
    sr = list(turn.service_requests.values())[-1]
    raw["routing"] = {"queue": sr["queue"], "request_id": sr["request_id"], "sla": sr["sla"]}
    cited = False
    for claim in raw["claims"]:
        if not isinstance(claim, dict):
            continue  # malformed model output: the schema check rejects it with a clear error instead of crashing
        for c in claim.get("citations") or []:
            if isinstance(c, dict) and c.get("kind") == "tool" and c.get("tool") == "raise_service_request":
                if c.get("request_id") not in turn.service_requests and len(turn.service_requests) == 1:
                    c["request_id"] = sr["request_id"]
                cited = True
    if not cited:
        cites = [{"kind": "tool", "tool": "raise_service_request", "request_id": sr["request_id"]}]
        if ("POL-SVC-006", "body") in turn.retrieved:
            cites.append({"kind": "policy", "doc_id": "POL-SVC-006", "section": "body", "quote": None})
        raw["claims"].append({"id": "routing", "type": "next_step", "citations": cites,
                              "text": f"I've raised a request with our {sr['queue']} team (reference {sr['request_id']}); "
                                      f"the target time is {sr['sla']}."})
    return raw


def _loop(turn: TurnContext, system: str, messages: list, emit) -> dict:
    deadline = time.monotonic() + config.agent_timeout_s
    repaired = False
    for _ in range(config.agent_max_iterations):
        if time.monotonic() > deadline:
            turn.trace.flag("agent_timeout")
            return safe_responses.validation_failed(turn)
        response = llm_router.call(system, TOOLS, messages, turn.trace, deadline)
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
                payload, errors = citation_validator.validate(_complete_routing(respond_block.input, turn), turn)
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
            log("citation_repair", level="warning", trace_id=turn.trace.trace_id, errors=errors[:5])
            turn.trace.validation_retries += 1
            turn.trace.flag("citation_repair")
            results.append({"type": "tool_result", "tool_use_id": respond_block.id, "is_error": True,
                            "content": "Your response failed validation. Fix every error and call respond again:\n- "
                                       + "\n- ".join(errors)})
        messages.append({"role": "user", "content": results})
    turn.trace.flag("max_iterations")
    log("agent_max_iterations", level="warning", trace_id=turn.trace.trace_id)
    return safe_responses.validation_failed(turn)


def _static(turn: TurnContext, message: str) -> dict:
    try:
        retrieval = retriever.search(message, turn.tenant_id)
        for ch in retrieval["chunks"]:
            turn.retrieved[(ch["doc_id"], ch["section"])] = ch
    except Exception:
        retrieval = None
    turn.trace.flag("static_mode")
    log("static_mode_answer", level="warning", trace_id=turn.trace.trace_id,
        grounded=bool(retrieval and retrieval["grounded"]))
    payload = safe_responses.static_mode(turn, retrieval)
    # The excerpt is verbatim policy text, so it still goes through the output guard (found in eval: for the
    # vulnerable customer EV-003 it showed "flagged under POL-CON-004 (vulnerable customer)"). Violation -> drop it.
    # WHY SimpleNamespace, not RespondPayload: a whole policy section can exceed the 500-char limit meant for
    # model-written claims, and validating it crashed static mode (eval EV-015/EV-017).
    view = SimpleNamespace(claims=[SimpleNamespace(text=c["text"], citations=[SimpleNamespace(**ci) for ci in c["citations"]])
                                   for c in payload["claims"]])
    if output_guard.check(view, turn):
        payload["claims"] = [c for c in payload["claims"] if c["type"] != "policy_fact"]
    return payload


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


def _conversation(conversation_id: str | None, customer_hash: str) -> tuple[str, list[dict]]:
    """Conversation memory lives in req_res_log (no separate table): the last turns with this conversation_id.
    An unknown id, or one that belongs to another customer, silently starts a new conversation."""
    import uuid

    if not conversation_id:
        return str(uuid.uuid4()), []
    try:
        from src.db.repositories import req_res_log_repo

        owner = req_res_log_repo.conversation_owner(conversation_id)
        if owner not in (None, customer_hash):
            return str(uuid.uuid4()), []  # WHY: never attach to (or read) another customer's thread
        msgs = []
        for t in req_res_log_repo.recent_turns(conversation_id, customer_hash, limit=2):
            if t["question"] and t["answer"]:
                msgs += [{"role": "user", "content": f"(earlier in this conversation) {t['question']}"},
                         {"role": "assistant", "content": f"(earlier in this conversation) {t['answer']}"}]
        return conversation_id, msgs
    except Exception:
        return conversation_id, []  # DB down: answer anyway, just without conversation memory


def run_agent(customer_id: str, message: str, tenant_id: str = "default",
              conversation_id: str | None = None, on_event=None) -> dict:
    """Raises CustomerNotFound (-> HTTP 404). Every other outcome is a structured response."""
    token = request_id_var.set(request_id_var.get())  # restored below: a script's per-turn id must not leak
    try:
        return _run_turn(customer_id, message, tenant_id, conversation_id, on_event)
    finally:
        reset_request_id(token)


def _run_turn(customer_id: str, message: str, tenant_id: str, conversation_id: str | None, on_event) -> dict:
    emit = on_event or (lambda *_: None)
    prompt = load_prompt()
    trace = Trace(tenant_id, customer_id, prompt, conversation_id)
    if trace.request_id is None or trace.request_id.startswith("local-"):
        # Called outside HTTP (demo, eval, tests): give every turn its own requestId so its log lines group.
        trace.request_id = f"local-{trace.trace_id[:8]}"
        set_request_id(trace.request_id)
    turn = TurnContext(customer_id, tenant_id, None, trace)
    # ENTRY guardrail: from here on `message` is the cleaned, PII-masked text (LLM, retrieval, DB, logs).
    guarded = input_guard.guard_input(message, customer_id)
    message, flags = guarded["message"], guarded["flags"]
    turn.input_flags = flags
    log("input_guarded", trace_id=trace.trace_id, flags=flags, pii=guarded["pii"], chars=len(message))
    for f in flags:
        trace.flag(f)

    turn.conversation_id, history = _conversation(conversation_id, trace.customer_id_hash)
    trace.conversation_id = turn.conversation_id

    try:
        with trace.span("request"):
            payload = _serve_from_cache(turn, message, prompt, flags, history)
            if payload is None:
                user_content = (f"<context>as_of_date: {as_of().isoformat()}\ninput_flags: {flags or 'none'}</context>\n"
                                f"<customer_message>\n{message}\n</customer_message>")
                # Retrieval-first: hybrid search on the question itself before the first model call. Saves one
                # model round-trip (the 25 s budget was blown by sequential calls in the eval). The model can
                # still search again with its own queries.
                try:
                    first = _run_tool("search_policy", {"query": message}, turn, emit)
                    user_content += f"\n<initial_policy_search query={message[:300]!r}>\n{first}\n</initial_policy_search>"
                except Exception:
                    pass  # vector store down: the loop / static mode handles it
                messages = history + [{"role": "user", "content": user_content}]
                try:
                    payload = _loop(turn, prompt["content"], messages, emit)
                except llm_router.LLMUnavailable:
                    payload = _static(turn, message)
                if _cacheable(payload, turn, flags, history):
                    _store_in_cache(message, turn, prompt, payload)
    except CustomerNotFound:
        trace.end("refused", error_code="CUSTOMER_NOT_FOUND")
        raise
    except Exception as e:
        # WHY: an unexpected crash must still leave a record (eval EV-017 crashed and left none).
        trace.flag(f"exception:{type(e).__name__}")
        log("agent_crashed", level="error", exc_info=True, trace_id=trace.trace_id, error=type(e).__name__, detail=str(e)[:300])
        trace.end("error", error_code="INTERNAL")
        raise

    final = _finalize(payload, turn)
    row = trace.end(final["status"], response=final, message=message)
    final.update(trace_id=trace.trace_id, conversation_id=turn.conversation_id, meta={
        "prompt_version": prompt["version"], "prompt_sha": prompt["sha256"][:12], "provider": row["provider"],
        "model": row["model_used"], "request_id": row["request_id"], "customer_id_hash": row["customer_id_hash"],
        "fallback_used": row["fallback_used"], "latency_ms": row["latency_ms"], "cache_hit": row["cache_hit"],
        "tokens": {"input": row["input_tokens"], "output": row["output_tokens"], "cache_read": row["cache_read_tokens"]},
        "cost_usd": row["cost_usd"], "llm_calls": row["llm_calls"], "flags": row["flags"]})
    # Not part of the HTTP response: the controller hands this to req_res_log (spans, LLM attempts, tokens).
    final["_internal"] = trace.log_fields(message=message)
    return final


def _serve_from_cache(turn: TurnContext, message: str, prompt: dict, flags: list, history: list) -> dict | None:
    """Semantic cache (Postgres): answer without the LLM, but only after re-checking the hit for THIS request."""
    if flags or history or not semantic_cache.enabled():
        return None
    try:
        with turn.trace.span("cache.lookup") as attrs:
            hit = semantic_cache.lookup(message, turn.tenant_id, prompt["version"])
            attrs.update(hit=bool(hit), match=hit and hit["match"], similarity=hit and hit["similarity"])
        if not hit:
            return None
        # 1. still grounded: every cited section must be retrieved again for the NEW question (local, no LLM)
        retrieval = retriever.search(message, turn.tenant_id)
        for ch in retrieval["chunks"]:
            turn.retrieved[(ch["doc_id"], ch["section"])] = ch
        for note in retrieval["conflicts"]:
            turn.conflicts[note["id"]] = note
        retrieved = {f"{d}|{s}" for d, s in turn.retrieved}
        if not retrieval["grounded"] or not set(hit["cited_sections"]) <= retrieved:
            log("semantic_cache_rejected", reason="citations_not_retrieved", match=hit["match"], trace_id=turn.trace.trace_id)
            return None
        # 2. this customer exists (404 otherwise) and the answer is safe for THEM (vulnerable -> no product talk)
        if run_get_account_state(turn) is ACCOUNT_STATE_UNAVAILABLE:
            return None
        payload = RespondPayload.model_validate(hit["answer"])
        if output_guard.check(payload, turn):
            log("semantic_cache_rejected", reason="output_guard", trace_id=turn.trace.trace_id)
            return None
    except CustomerNotFound:
        raise
    except Exception as e:  # cache trouble must never break an answer
        log("semantic_cache_unavailable", level="warning", error=type(e).__name__, detail=str(e)[:160])
        return None
    turn.trace.cache_hit = True
    turn.trace.flag(f"cache_hit_{hit['match']}")
    log("semantic_cache_hit", match=hit["match"], similarity=hit["similarity"], cached_question=hit["question_norm"][:80],
        trace_id=turn.trace.trace_id)
    try:
        from src.db.repositories import semantic_cache_repo

        semantic_cache_repo.record_hit(hit["cache_key"])
    except Exception:
        pass
    return json.loads(json.dumps(hit["answer"]))


def _store_in_cache(message: str, turn: TurnContext, prompt: dict, payload: dict) -> None:
    try:
        semantic_cache.store(message, turn.tenant_id, prompt["version"], payload, turn.trace.model_used)
        log("semantic_cache_stored", trace_id=turn.trace.trace_id)
    except Exception as e:
        log("semantic_cache_store_failed", level="warning", error=type(e).__name__, detail=str(e)[:160])


def _cacheable(payload: dict, turn: TurnContext, flags: list, history: list) -> bool:
    # WHY so strict: a cached answer is served to *other customers*. Anything that touched account state,
    # raised a request, needed a repair, missed retrieval or came from a degraded path must never be reused.
    quality_flags = {f for f in turn.trace.flags if f not in _OPERATIONAL_FLAGS and not f.startswith("circuit_open:")}
    return (semantic_cache.enabled() and payload["status"] == "answered" and not turn.account_attempted
            and not turn.service_requests and not flags and not history and not quality_flags)


# Which provider answered, or a wait for a rate limit, says nothing about the answer's quality: it passed the
# same validator and guards. Anything else (repair, retrieval miss, static mode, timeouts) blocks caching.
_OPERATIONAL_FLAGS = {"fallback_used", "rate_limit_wait"}
