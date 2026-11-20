"""Canned responses for degraded paths. Deterministic text written by us (not the model), always in the
same structured shape, so the customer still gets an honest, routed answer."""
from src.const.categories import CATEGORIES
from src.const.statuses import DEGRADED, REFUSED


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
