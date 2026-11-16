"""Canned responses for degraded paths. Deterministic text written by us (not the model), always in the
same structured shape, so the customer still gets an honest, routed answer."""
from src.const.categories import CATEGORIES
from src.const.statuses import ANSWERED, DEGRADED, REFUSED


def _route_general(turn, summary: str) -> dict | None:
    """Best effort: hand the question to a human. Never raises — a degraded path must not crash."""
    from src.modules.tools.errors import CustomerNotFound
    from src.modules.tools.tool_runner import run_raise_service_request

    try:
        result = run_raise_service_request(turn, "GENERAL", summary)
        return result if "request_id" in result else None
    except CustomerNotFound:
        raise  # unknown customer is a 404, not a degraded answer
    except Exception:
        return None


def _routing(sr: dict | None) -> dict | None:
    if not sr:
        return None
    return {"queue": sr["queue"], "request_id": sr["request_id"], "sla": CATEGORIES[sr["queue"]]["sla"]}


def _handoff_claim(cid: str, sr: dict | None) -> dict:
    if sr:
        return {"id": cid, "type": "next_step",
                "text": f"I've passed your question to our team (reference {sr['request_id']}); "
                        f"a person will review it within 1 working day.",
                "citations": [{"kind": "tool", "tool": "raise_service_request", "request_id": sr["request_id"]}]}
    return {"id": cid, "type": "next_step",
            "text": "I couldn't confirm a request was submitted for you, so please don't assume it was — "
                    "please contact us or try again shortly.",
            "citations": [{"kind": "tool", "tool": "raise_service_request", "request_id": None}]}


def account_unavailable(turn) -> dict:
    """Spec §12: what the customer sees when the account tool times out — honest, nothing guessed."""
    sr = _route_general(turn, "Account details unavailable (upstream timeout); customer question needs follow-up.")
    return {
        "status": DEGRADED, "language": "en",
        "claims": [
            {"id": "c1", "type": "next_step",
             "text": "I couldn't retrieve your account details just now, so I can't check that for you. "
                     "Nothing has been changed on your account.",
             "citations": [{"kind": "tool", "tool": "get_account_state", "request_id": None}]},
            _handoff_claim("c2", sr),
        ],
        "decision": {"outcome": "none", "reason_code": None},
        "routing": _routing(sr), "unanswered": [], "conflicts_noted": [],
    }


def validation_failed(turn) -> dict:
    if turn.account_unavailable:
        return account_unavailable(turn)  # the real cause is the upstream timeout — say so (found in eval EV-028)
    sr = _route_general(turn, "Customer question could not be answered reliably by the assistant; needs human review.")
    return {
        "status": REFUSED, "language": "en",
        "claims": [
            {"id": "c1", "type": "gap",
             "text": "I'm sorry — I can't give you a reliable answer to that right now, so I won't guess.",
             "citations": [{"kind": "retrieval_miss", "query": "answer could not be verified"}]},
            _handoff_claim("c2", sr),
        ],
        "decision": {"outcome": "none", "reason_code": None},
        "routing": _routing(sr), "unanswered": ["answer could not be verified"], "conflicts_noted": [],
    }


def static_mode(turn, retrieval: dict | None, question: str = "") -> dict:
    """All LLMs down. Exit rules (see static_answers.py), strictest first:
    1. a plain account lookup (due date, balance, minimum due, limit) -> answered from the customer's record, cited;
    2. else the policy LINES that actually cover the question's key terms -> shown, cited, routed to a human;
    3. else nothing but an honest "can't answer right now" + routed to a human (never an unrelated policy dump)."""
    from src.helper.text import normalise
    from src.modules.agent import static_answers
    from src.modules.tools.tool_runner import ACCOUNT_STATE_UNAVAILABLE, run_get_account_state

    flagged = set(turn.input_flags) & static_answers.REFUSE_FLAGS
    if flagged:
        turn.trace.flag("static_refused")
        sr = _route_general(turn, "Assistant unavailable; flagged request needs human review.")
        return {"status": REFUSED, "language": "en",
                "claims": [{"id": "c1", "type": "next_step",
                            "text": "I can't help with that request here. Our assistant is also temporarily unavailable.",
                            "citations": [{"kind": "tool", "tool": "static_mode", "request_id": None}]},
                           _handoff_claim("c2", sr)],
                "decision": {"outcome": "none", "reason_code": None}, "routing": _routing(sr),
                "unanswered": [], "conflicts_noted": []}

    intent = static_answers.match_account_intent(question)
    if intent:
        name, fields = intent
        state = run_get_account_state(turn)  # CustomerNotFound propagates -> 404
        if state is ACCOUNT_STATE_UNAVAILABLE:
            return account_unavailable(turn)
        text = static_answers.render_account_answer(name, turn.raw_account)
        if text:
            turn.trace.flag("static_account_answer")
            return {"status": ANSWERED, "language": "en",
                    "claims": [{"id": "c1", "type": "account_fact", "text": text,
                                "citations": [{"kind": "account", "field": f} for f in fields]}],
                    "decision": {"outcome": "none", "reason_code": None}, "routing": None,
                    "unanswered": [], "conflicts_noted": []}

    sr = _route_general(turn, "Assistant unavailable (model outage); customer question needs human review.")
    excerpt, ch = None, None
    action = static_answers.is_action_request(question)
    if retrieval and retrieval.get("grounded") and not action:
        # Never show a conflict's superseded document on its own (EV-012 showed POL-CRD-011's 3-month rule).
        losing = {d for n in retrieval.get("conflicts", []) for d in n["docs"] if d != n["prevails"]}
        q_norm = normalise(question)
        vocabulary = static_answers.corpus_vocabulary(turn.tenant_id)
        for c in [c for c in retrieval["chunks"] if c["doc_id"] not in losing]:
            # WHY whole lines, never cut mid-line: a truncated excerpt once turned "2.5%" into "2.5".
            excerpt = static_answers.relevant_excerpt(q_norm, c["text"], vocabulary)
            if excerpt:
                ch = c
                break
    if excerpt:
        claims = [{"id": "c1", "type": "next_step",
                   "text": "I can't answer fully right now because our assistant is temporarily unavailable.",
                   "citations": [{"kind": "tool", "tool": "static_mode", "request_id": None}]},
                  {"id": "c2", "type": "policy_fact",
                   "text": f"The relevant part of our policy ({ch['doc_id']} §{ch['section']}) says:\n{excerpt}",
                   "citations": [{"kind": "policy", "doc_id": ch["doc_id"], "section": ch["section"], "quote": None}]}]
    else:
        turn.trace.flag("static_action_request" if action else "static_no_relevant_excerpt")
        claims = [{"id": "c1", "type": "next_step",
                   "text": ("I can't do that for you right now because our assistant is temporarily unavailable."
                            if action else
                            "I can't answer that right now because our assistant is temporarily unavailable, "
                            "and I won't guess."),
                   "citations": [{"kind": "tool", "tool": "static_mode", "request_id": None}]}]
    claims.append(_handoff_claim(f"c{len(claims) + 1}", sr))
    return {"status": DEGRADED, "language": "en", "claims": claims,
            "decision": {"outcome": "none", "reason_code": None}, "routing": _routing(sr),
            "unanswered": [], "conflicts_noted": []}


def out_of_scope(question: str) -> dict:
    """Entry scope guard: the question is not about this card service at all (decided from the policy documents'
    vectors + vocabulary, see guardrails/scope_guard.py). Fixed text, no LLM call, no human ticket."""
    return {
        "status": REFUSED, "language": "en",
        "claims": [{"id": "c1", "type": "gap",
                    "text": "I can only help with questions about your credit card account and our card policies, "
                            "so I can't help with that one. Is there anything about your card I can help with?",
                    "citations": [{"kind": "retrieval_miss", "query": question[:300]}]}],
        "decision": {"outcome": "none", "reason_code": None}, "routing": None,
        "unanswered": [question[:200]], "conflicts_noted": [],
    }
