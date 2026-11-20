"""The tool-calling loop. One call to run_agent() = one customer turn (spec §4 request lifecycle)."""
import json
import time
from dataclasses import dataclass, field

from src.const.config import config
from src.helper.async_utils import with_timeout
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
    tool_errors: dict = field(default_factory=dict)  # tool name -> failures this turn (ReAct retry budget)


def _wrap(payload: dict) -> str:
    return f"<tool_output>\n{json.dumps(payload, default=str)}\n</tool_output>"


# --- ReAct observations for failures ----------------------------------------------------------------------------
# A failing tool (Chroma down, retrieval timeout, a bug in a rule, an unexpected upstream error) must not crash the
# turn. The failure goes back to the model as an OBSERVATION; the model reasons about it and acts: retry once,
# reword the search, or answer honestly with what it already has (status degraded). Retries of *transient* errors
# (timeouts, backoff, circuit breakers) already happen inside tool_runner / the embedder before this point.
MAX_TOOL_ERRORS = 2  # per tool per turn: 1st failure -> the model may retry once; 2nd -> stop calling it
_NO_RETRY_TOOLS = {"raise_service_request"}  # a failed call may already have created the request: never retry blindly
_ACCOUNT_TOOLS = {"get_account_state", "check_limit_increase_eligibility"}


def _record_tool_error(turn: "TurnContext", name: str, error: Exception) -> bool:
    """Count the failure, flag it, log it. Returns True if the model may call this tool again in this turn."""
    turn.tool_errors[name] = turn.tool_errors.get(name, 0) + 1
    if name in _ACCOUNT_TOOLS and turn.raw_account is None:
        turn.account_unavailable = True  # the validator then rejects any account_fact claim
    retryable = name not in _NO_RETRY_TOOLS and turn.tool_errors[name] < MAX_TOOL_ERRORS
    turn.trace.flag(f"tool_error:{name}")
    log("tool_error", level="warning", tool=name, error=type(error).__name__, detail=str(error)[:160],
        failures=turn.tool_errors[name], retryable=retryable, trace_id=turn.trace.trace_id)
    return retryable


def _tool_error_observation(name: str, error: str, retryable: bool) -> str:
    if name == "raise_service_request":
        instruction = ("The request could not be confirmed: it may or may not exist. Do not call raise_service_request "
                       "again. Tell the customer you couldn't confirm it was submitted and they should not assume it was.")
    elif retryable:
        instruction = (f"{name} failed. You may call it once more (search_policy: you may reword the query). "
                       "If it fails again, do not guess what it would have returned.")
    else:
        instruction = (f"{name} is unavailable for the rest of this turn: do not call it again and do not guess its "
                       "result. Answer only what you can cite from results you already have, list what you could not "
                       "check in 'unanswered', and use status degraded.")
    return _wrap({"error": "TOOL_ERROR", "tool": name, "error_type": error, "retryable": retryable,
                  "instruction": instruction})


def _args(block) -> dict:
    """Tool arguments without the ReAct `thought` (a copy: the provider's original call part is resent unchanged)."""
    return {k: v for k, v in (block.input or {}).items() if k != "thought"}


def _observe(block, turn: "TurnContext", emit) -> dict:
    """ReAct step: run one tool call and return its observation (a tool_result block) — never raise, except
    CustomerNotFound (an unknown customer is a 404, not something the model can recover from)."""
    name = block.name
    if turn.tool_errors.get(name, 0) >= MAX_TOOL_ERRORS or (name in _NO_RETRY_TOOLS and turn.tool_errors.get(name)):
        # retry budget used up: answer from the budget, do not call the dependency again
        return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                "content": _tool_error_observation(name, "RetryBudgetExhausted", retryable=False)}
    try:
        return {"type": "tool_result", "tool_use_id": block.id, "content": _run_tool(name, _args(block), turn, emit)}
    except CustomerNotFound:
        raise
    except Exception as e:
        retryable = _record_tool_error(turn, name, e)
        return {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                "content": _tool_error_observation(name, type(e).__name__, retryable)}


def _run_tool(name: str, args: dict, turn: TurnContext, emit) -> str:
    if name == "search_policy":
        emit("stage", {"stage": "retrieving"})
        query = str(args.get("query", ""))[:300]
        with turn.trace.span("retrieval.hybrid", query=query) as attrs:
            # WHY a timeout here: retrieval calls the embedding API and Chroma; a hang must become an observation
            # the model can react to, not eat the whole 25 s budget.
            result = with_timeout(retriever.search, config.retrieval_timeout_s, query, turn.tenant_id)
            top = result["chunks"][0] if result["chunks"] else None
            attrs.update(grounded=result["grounded"], top_vec_sim=result["top_vec_sim"],
                         embedding_model=result.get("embedding_model"),
                         top=f"{top['doc_id']}§{top['section']}" if top else None,
                         rrf=top["scores"]["rrf"] if top else None, cache_hit=result["cache_hit"],
                         conflicts=[c["id"] for c in result["conflicts"]])
        for ch in result["chunks"]:
            turn.retrieved[(ch["doc_id"], ch["section"])] = ch
        for note in result["conflicts"]:
            turn.conflicts[note["id"]] = note
        _flag_embedding(turn, result.get("embedding_model"))
        log("policy_search", grounded=result["grounded"], top_vec_sim=result["top_vec_sim"],
            embedding_model=result.get("embedding_model"),
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


def _flag_embedding(turn: TurnContext, model: str | None) -> None:
    """Visible in req_res_log.flags: which questions ran on a fallback embedding model, or on BM25 alone."""
    from src.modules.retrieval import embedder

    if model is None:
        turn.trace.flag("embedding_unavailable")
    elif model != embedder.models()[0]:
        turn.trace.flag(f"embedding_fallback:{model}")


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


FINISH_ONLY = [t for t in TOOLS if t["name"] == "respond"]


def _loop_state(step: int, max_steps: int, deadline: float) -> str:
    """What the agent knows about its own budget, appended to every observation so it can plan (ReAct needs the
    agent to decide *when to stop*, not only what to call)."""
    left = max(0, int(deadline - time.monotonic()))
    state = f'<loop_state step="{step}" max_steps="{max_steps}" seconds_left="{left}"/>'
    if step + 1 >= max_steps:
        state += ("\nThe next step is your LAST: only the respond tool is available. Answer with what you can cite "
                  "now; list anything you could not check in 'unanswered'.")
    return state


def _thought(response) -> str | None:
    """The REASON part of a step: the `thought` argument of each tool call (required by every tool schema), plus any
    free text the model wrote next to them. Recorded in the trace only — never shown to the customer (customer text
    comes from validated claims)."""
    parts = [b.text for b in response.content if b.type == "text" and getattr(b, "text", None)]
    parts += [f"[{b.name}] {b.input['thought']}" for b in response.content
              if b.type == "tool_use" and isinstance(b.input, dict) and b.input.get("thought")]
    return " | ".join(p.strip() for p in parts)[:500] or None


def _finish(respond_block, turn: TurnContext, emit) -> tuple[dict | None, list[str]]:
    """FINISH action: the respond tool. Code checks it before anything reaches the customer."""
    emit("stage", {"stage": "validating"})
    with turn.trace.span("validate.citations") as attrs:
        payload, errors = citation_validator.validate(_complete_routing(_args(respond_block), turn), turn)
        attrs.update(ok=not errors, errors=errors[:10])
    if payload is not None and not errors:
        with turn.trace.span("guard.output") as attrs:
            errors = output_guard.check(payload, turn)
            attrs.update(ok=not errors, violations=errors)
    return (payload.model_dump() if payload is not None and not errors else None), errors


def _loop(turn: TurnContext, system: str, messages: list, emit) -> dict:
    """ReAct loop: Reason -> Act -> Observe, repeated until the agent FINISHES (respond) or the budget is spent.

      REASON   the model writes a short thought and decides the next action(s)          (llm_router.call)
      ACT      one or more tool calls in the same step (parallel); `respond` = finish     (_observe / _finish)
      OBSERVE  every result goes back as an observation — successes, TOOL_ERROR, a failed validation —
               plus <loop_state> (steps and seconds left), so the agent can re-plan
      FINISH   respond -> citation validator -> output guard; rejected once -> the errors are the observation
      BUDGET   max AGENT_MAX_ITERATIONS steps, AGENT_TIMEOUT_MS; the last step offers only `respond` (forced finish)
    Each step is one `agent.step` span in the trace: thought, actions, observations, outcome.
    """
    deadline = time.monotonic() + config.agent_timeout_s
    max_steps = config.agent_max_iterations
    repaired = False
    for step in range(1, max_steps + 1):
        if time.monotonic() > deadline:
            turn.trace.flag("agent_timeout")
            return safe_responses.validation_failed(turn)
        last = step == max_steps
        with turn.trace.span("agent.step", step=step, max_steps=max_steps) as attrs:
            # REASON + ACT (the model chooses). On the last step only `respond` is offered: forced finish.
            response = llm_router.call(system, FINISH_ONLY if last else TOOLS, messages, turn.trace, deadline)
            messages.append({"role": "assistant", "content": response.content})  # unchanged (keeps thinking valid)
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            attrs.update(thought=_thought(response), actions=[b.name for b in tool_uses] or None)
            if not tool_uses:  # a thought without an action: ask for one
                attrs["outcome"] = "no_action"
                messages.append({"role": "user", "content": NUDGE + "\n" + _loop_state(step, max_steps, deadline)})
                continue

            # OBSERVE: every non-finish action runs and returns an observation (failures included, never raised)
            results = [_observe(b, turn, emit) for b in tool_uses if b.name != "respond"]
            attrs["observations"] = [{"tool": b.name, "ok": not r.get("is_error")}
                                     for b, r in zip([b for b in tool_uses if b.name != "respond"], results, strict=True)]
            respond_block = next((b for b in tool_uses if b.name == "respond"), None)

            if respond_block is not None:  # FINISH
                answer, errors = _finish(respond_block, turn, emit)
                if answer is not None:
                    attrs["outcome"] = "finished"
                    return answer
                if repaired:
                    attrs["outcome"] = "rejected_again"
                    turn.trace.flag("citation_validation_failed")
                    log("validation_failed", level="warning", trace_id=turn.trace.trace_id, errors=errors[:5])
                    return safe_responses.validation_failed(turn)
                repaired = True
                attrs["outcome"] = "rejected_repair"
                log("citation_repair", level="warning", trace_id=turn.trace.trace_id, errors=errors[:5])
                turn.trace.validation_retries += 1
                turn.trace.flag("citation_repair")
                results.append({"type": "tool_result", "tool_use_id": respond_block.id, "is_error": True,
                                "content": "Your response failed validation. Fix every error and call respond again:\n- "
                                           + "\n- ".join(errors)})
            else:
                attrs["outcome"] = "observed"
            messages.append({"role": "user", "content": results + [
                {"type": "text", "text": _loop_state(step, max_steps, deadline)}]})
    turn.trace.flag("max_iterations")
    log("agent_max_iterations", level="warning", trace_id=turn.trace.trace_id)
    return safe_responses.validation_failed(turn)


class LLMLimitReached(Exception):
    """Every model in the chain failed (quota exhausted, outage, or LLM_DISABLED). There is no answer without a model:
    the API returns 503 LLM_LIMIT_REACHED ("try again tomorrow"). `internal` carries the trace for req_res_log, so
    every failed model attempt is still recorded."""

    def __init__(self, internal: dict):
        super().__init__("all LLMs unavailable")
        self.internal = internal


def _no_model_available(turn: TurnContext, message: str) -> LLMLimitReached:
    # An unknown customer is still a 404, not a quota message (CustomerNotFound propagates from here).
    if turn.raw_account is None and not turn.account_unavailable:
        run_get_account_state(turn)
    log("llm_limit_reached", level="error", trace_id=turn.trace.trace_id)
    turn.trace.end("error", error_code="LLM_LIMIT_REACHED", message=message)
    return LLMLimitReached(turn.trace.log_fields(message=message))


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
            payload = _out_of_scope(turn, message, history)
            if payload is None:
                payload = _serve_from_cache(turn, message, prompt, flags, history)
            if payload is None:
                user_content = (f"<context>as_of_date: {as_of().isoformat()}\ninput_flags: {flags or 'none'}</context>\n"
                                f"<customer_message>\n{message}\n</customer_message>")
                # Pre-fetched OBSERVATION (AGENT_PREFETCH_SEARCH=1, default): code runs one search on the question
                # itself before the first REASON step. The only action the agent does not choose — WHY: it saves one
                # model round-trip (sequential calls blew the 25 s budget in the eval). The agent still decides
                # whether to search again with its own wording. AGENT_PREFETCH_SEARCH=0 = pure ReAct.
                if config.agent_prefetch_search:
                    try:
                        first = _run_tool("search_policy", {"query": message}, turn, emit)
                        user_content += f"\n<initial_policy_search query={message[:300]!r}>\n{first}\n</initial_policy_search>"
                    except Exception as e:
                        # Vector store / embedding down: tell the model (an observation) instead of hiding it.
                        _record_tool_error(turn, "search_policy", e)
                        user_content += (f"\n<initial_policy_search query={message[:300]!r} status=\"failed\" "
                                         f"error=\"{type(e).__name__}\">Policy search failed. Call search_policy yourself "
                                         f"(one retry is allowed); if it fails again, do not guess.</initial_policy_search>")
                messages = history + [{"role": "user", "content": user_content}]
                try:
                    payload = _loop(turn, prompt["content"], messages, emit)
                except llm_router.LLMUnavailable:
                    raise _no_model_available(turn, message) from None
                if _cacheable(message, payload, turn, flags, history):
                    _store_in_cache(message, turn, prompt, payload)
    except CustomerNotFound:
        trace.end("refused", error_code="CUSTOMER_NOT_FOUND")
        raise
    except LLMLimitReached:
        raise  # already recorded by _no_model_available
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


def _out_of_scope(turn: TurnContext, message: str, history: list) -> dict | None:
    """ENTRY scope guard (tier 2): an off-domain question gets a fixed reply — no LLM, no ticket.
    Skipped inside a conversation: a short follow-up ("and for my Classic card?") is only meaningful with its history."""
    if history:
        return None
    from src.helper.text import normalise
    from src.modules.guardrails import scope_guard

    with turn.trace.span("guard.scope") as attrs:
        result = scope_guard.check(normalise(message), turn.tenant_id)
        attrs.update(**{k: result[k] for k in ("in_scope", "sim", "corpus_word_share", "reason")})
    if result["in_scope"]:
        return None
    turn.trace.flag("out_of_scope")
    log("out_of_scope", sim=result["sim"], corpus_word_share=result["corpus_word_share"], trace_id=turn.trace.trace_id)
    return safe_responses.out_of_scope(message)


def _serve_from_cache(turn: TurnContext, message: str, prompt: dict, flags: list, history: list) -> dict | None:
    """Semantic cache (Postgres): answer without the LLM, but only after re-checking the hit for THIS request."""
    if flags or history or not semantic_cache.enabled():
        return None
    if semantic_cache.is_personal(message):
        # The cache is SHARED by all customers: a question about "my" account always goes to the agent, which reads
        # THIS customer's record. Never answered from (or written to) the cache.
        turn.trace.flag("cache_bypass_personal")
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


def _cacheable(message: str, payload: dict, turn: TurnContext, flags: list, history: list) -> bool:
    """WHY so strict: a cached answer is served to *other customers*. Three independent checks, all must pass:
      1. question  : not personal ("my", "I", a customer id, masked PII ...)      -> semantic_cache.is_personal
      2. execution : no account tool called, no service request, no input flags, no history, no repair / miss /
                     degraded / tool error                                          -> this function
      3. answer    : no account_fact claim, no account / tool citation, no routing  -> semantic_cache.is_account_independent
    """
    quality_flags = {f for f in turn.trace.flags if f not in _OPERATIONAL_FLAGS
                     and not f.startswith(("circuit_open:", "embedding_fallback:"))}
    return (semantic_cache.enabled() and payload["status"] == "answered"
            and not semantic_cache.is_personal(message)
            and not turn.account_attempted and not turn.service_requests and not flags and not history
            and not quality_flags and semantic_cache.is_account_independent(payload))


# Which provider answered, or a wait for a rate limit, says nothing about the answer's quality: it passed the
# same validator and guards. Anything else (repair, retrieval miss, tool errors, timeouts) blocks caching.
_OPERATIONAL_FLAGS = {"fallback_used", "rate_limit_wait"}
