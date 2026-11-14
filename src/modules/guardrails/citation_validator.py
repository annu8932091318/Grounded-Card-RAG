"""THE enforcement point (spec §10.4). Deterministic code checks every claim against what was actually
retrieved / returned in THIS turn. Returns (parsed_payload | None, errors). Empty errors = valid."""
import json

from pydantic import ValidationError

from src.const.conflicts import CONFLICTS
from src.const.reason_codes import REASON_CODES
from src.helper.text import extract_numbers, normalise
from src.modules.agent.respond_schema import RespondPayload

MAX_QUOTE_WORDS = 25


def _numbers_of(value) -> set[str]:
    return extract_numbers(value if isinstance(value, str) else json.dumps(value, default=str))


def _check_citation(cit, claim, turn, payload, errors: list[str]) -> set[str]:
    """Validate one citation; return the numbers it grounds."""
    cid = claim.id
    if cit.kind == "policy":
        chunk = turn.retrieved.get((cit.doc_id, cit.section))
        if chunk is None:
            errors.append(f"{cid}: policy citation {cit.doc_id} §{cit.section} was not returned by search_policy "
                          f"in this turn (quarantined/non-policy documents can never be cited)")
            return set()
        if cit.quote:
            if len(cit.quote.split()) > MAX_QUOTE_WORDS:
                errors.append(f"{cid}: quote longer than {MAX_QUOTE_WORDS} words")
            elif normalise(cit.quote) not in normalise(chunk["text"]):
                errors.append(f"{cid}: quote is not verbatim from {cit.doc_id} §{cit.section}")
        return _numbers_of(chunk["text"])

    if cit.kind == "account":
        source = {**(turn.account_state or {}), **(turn.facts or {})}
        if turn.account_state is None or cit.field not in source:
            errors.append(f"{cid}: account field '{cit.field}' was not returned by get_account_state in this turn")
            return set()
        return _numbers_of(source[cit.field])

    if cit.kind == "tool":
        if cit.tool == "raise_service_request":
            sr = turn.service_requests.get(cit.request_id or "")
            if sr is None:
                errors.append(f"{cid}: request_id {cit.request_id} was not returned by raise_service_request")
                return set()
            return _numbers_of(sr)
        if cit.tool == "get_account_state":
            if not turn.account_attempted:
                errors.append(f"{cid}: get_account_state was not called in this turn")
            return set()
        if cit.tool == "check_limit_increase_eligibility":
            if turn.eligibility is None:
                errors.append(f"{cid}: check_limit_increase_eligibility was not called in this turn")
                return set()
            return _numbers_of(turn.eligibility)
        errors.append(f"{cid}: unknown tool '{cit.tool}' in citation")
        return set()

    # retrieval_miss
    if claim.type != "gap":
        errors.append(f"{cid}: retrieval_miss citations are only allowed on gap claims")
    ran_and_missed = normalise(cit.query) in {normalise(q) for q in turn.misses}
    if not ran_and_missed and not payload.unanswered:
        errors.append(f"{cid}: retrieval_miss query '{cit.query}' was not run with grounded=false and "
                      f"'unanswered' is empty")
    return set()


def validate(raw_payload: dict, turn) -> tuple[RespondPayload | None, list[str]]:
    # 1. schema
    try:
        payload = RespondPayload.model_validate(raw_payload)
    except ValidationError as e:
        return None, [f"schema: {'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:10]]

    errors: list[str] = []
    derived = _numbers_of(turn.facts) if turn.facts else set()
    cited_docs: set[str] = set()

    for claim in payload.claims:
        # 2. every claim cited
        if not claim.citations:
            errors.append(f"{claim.id}: claim has no citation")
            continue
        grounded_numbers = set(derived)
        for cit in claim.citations:
            grounded_numbers |= _check_citation(cit, claim, turn, payload, errors)  # 3-6, 8
            if cit.kind == "policy":
                cited_docs.add(cit.doc_id)
        # 7. number grounding — catches invented fees/rates/days
        ungrounded = extract_numbers(claim.text) - grounded_numbers
        if ungrounded:
            errors.append(f"{claim.id}: number(s) {sorted(ungrounded)} do not appear in any cited source")
        # 8. gap claims need evidence of the gap
        if claim.type == "gap" and not any(c.kind == "retrieval_miss" for c in claim.citations) and not payload.unanswered:
            errors.append(f"{claim.id}: gap claim must cite a retrieval_miss or list the topic in 'unanswered'")

    # 9. decision / reason codes (D4: never invent or misuse a code)
    code = payload.decision.reason_code
    if code is not None:
        if code not in REASON_CODES:
            errors.append(f"decision: reason_code {code} is not an approved code (AA01-AA06 only)")
        elif turn.eligibility is None:
            errors.append("decision: a reason_code requires check_limit_increase_eligibility in this turn")
        elif code not in turn.eligibility["applicable_reason_codes"]:
            errors.append(f"decision: reason_code {code} is not supported by the eligibility result "
                          f"(applicable: {turn.eligibility['applicable_reason_codes'] or 'none'})")
    if payload.decision.outcome == "declined" and code is None and not any(c.type == "gap" for c in payload.claims):
        errors.append("decision: a decline needs an approved reason code or a gap claim saying none applies")
    if turn.eligibility and payload.decision.outcome != "none" and payload.decision.outcome != turn.eligibility["outcome"]:
        errors.append(f"decision: outcome must match the eligibility result ({turn.eligibility['outcome']})")

    # 10. status consistency
    s = payload.status
    if s == "request_raised":
        rid = payload.routing.request_id if payload.routing else None
        if rid not in turn.service_requests:
            errors.append("status request_raised requires routing.request_id returned by raise_service_request")
        if not any(c.kind == "tool" and c.tool == "raise_service_request" for cl in payload.claims for c in cl.citations):
            errors.append("status request_raised requires a claim citing the raise_service_request result")
    if payload.routing and payload.routing.request_id and payload.routing.request_id not in turn.service_requests:
        errors.append("routing.request_id was not returned by raise_service_request")
    if s == "declined" and payload.decision.outcome != "declined":
        errors.append("status declined requires decision.outcome = declined")
    if s == "answered_with_gap" and not any(c.type == "gap" for c in payload.claims):
        errors.append("status answered_with_gap requires at least one gap claim")
    if turn.account_unavailable and any(c.type == "account_fact" for c in payload.claims):
        errors.append("account details were unavailable this turn; account_fact claims are not allowed")

    # conflicts: never rely on the losing document alone
    for c in CONFLICTS:
        losing = [d for d in c["docs"] if d != c["prevails"]]
        if c["id"] in turn.conflicts and any(d in cited_docs for d in losing) and c["prevails"] not in cited_docs:
            errors.append(f"conflict {c['id']}: cites {losing} without the prevailing {c['prevails']}")

    return payload, errors
