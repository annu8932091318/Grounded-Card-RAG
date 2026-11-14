"""The validator must reject uncited, fabricated and mis-numbered claims (and accept a correct one)."""
import copy

import pytest

from src.modules.decision.facts import compute_facts
from src.modules.decision.limit_increase_rules import evaluate
from src.modules.guardrails.citation_validator import validate
from src.modules.guardrails.redact import redact_account

FEE_TEXT = ("POL-FEE-007 Fees and Charges Schedule §Table: | Charge | Amount |\n"
            "| Annual card fee, Signature | Waived on spend of 4,00,000 in the card year |\n"
            "| Foreign currency markup | 3.5% |")


@pytest.fixture
def turn(make_turn, accounts):
    t = make_turn("CUST-2000")
    t.retrieved[("POL-FEE-007", "Table")] = {"doc_id": "POL-FEE-007", "section": "Table", "text": FEE_TEXT}
    t.raw_account = accounts["CUST-2000"]
    t.account_state = redact_account(t.raw_account)
    t.account_attempted = True
    t.facts = compute_facts(t.raw_account)
    return t


GOOD = {
    "status": "answered", "language": "en",
    "claims": [
        {"id": "c1", "type": "policy_fact", "text": "The foreign currency markup is 3.5%.",
         "citations": [{"kind": "policy", "doc_id": "POL-FEE-007", "section": "Table", "quote": "Foreign currency markup | 3.5%"}]},
        {"id": "c2", "type": "account_fact", "text": "Your card-year spend is 1,62,000, below the 4,00,000 waiver threshold.",
         "citations": [{"kind": "account", "field": "card_year_spend"},
                       {"kind": "policy", "doc_id": "POL-FEE-007", "section": "Table", "quote": None}]},
    ],
    "decision": {"outcome": "none", "reason_code": None}, "routing": None, "unanswered": [], "conflicts_noted": [],
}


def _with(**changes):
    p = copy.deepcopy(GOOD)
    for path, value in changes.items():
        target = p
        *keys, last = path.split("__")
        for k in keys:
            target = target[int(k)] if k.isdigit() else target[k]
        target[int(last) if last.isdigit() else last] = value
    return p


def test_valid_payload_passes(turn):
    _, errors = validate(GOOD, turn)
    assert errors == []


def test_rejects_uncited_claim(turn):
    _, errors = validate(_with(claims__0__citations=[]), turn)
    assert any("no citation" in e for e in errors)


def test_rejects_fabricated_document(turn):
    bad = [{"kind": "policy", "doc_id": "POL-TRV-099", "section": "1", "quote": None}]
    _, errors = validate(_with(claims__0__citations=bad), turn)
    assert any("not returned by search_policy" in e for e in errors)


def test_rejects_non_policy_citation_even_if_named(turn):
    bad = [{"kind": "policy", "doc_id": "POL-OPS-012", "section": "body", "quote": None}]
    _, errors = validate(_with(claims__0__citations=bad), turn)
    assert errors


def test_rejects_wrong_number(turn):
    _, errors = validate(_with(claims__0__text="The foreign currency markup is 4.5%."), turn)
    assert any("4.5" in e for e in errors)


def test_non_verbatim_quote_is_dropped_never_shown(turn):
    bad = [{"kind": "policy", "doc_id": "POL-FEE-007", "section": "Table", "quote": "markup is waived for Signature"}]
    payload, errors = validate(_with(claims__0__citations=bad), turn)
    assert errors == [] and payload.claims[0].citations[0].quote is None
    assert "quote_dropped" in turn.trace.flags


def test_citation_identifier_formatting_is_normalised(turn):
    messy = [{"kind": "policy", "doc_id": "POL-FEE-007, section:", "section": "§Table", "quote": None}]
    _, errors = validate(_with(claims__0__citations=messy), turn)
    assert errors == []
    junk = [{"kind": "policy", "doc_id": "POL-FEE-007", "section": "Table}],id:", "quote": None}]  # eval EV-006
    _, errors = validate(_with(claims__0__citations=junk), turn)
    assert errors == []


def test_invented_multiplier_is_caught(turn):
    _, errors = validate(_with(claims__0__text="Your limit can go up to 3x."), turn)
    assert any("'3'" in e for e in errors)


def test_injection_attempt_must_be_refused(turn):
    turn.input_flags = ["injection_suspected"]
    _, errors = validate(GOOD, turn)
    assert any("must be refused" in e for e in errors)
    _, errors = validate(_with(status="refused"), turn)
    assert errors == []


def test_decline_must_cite_the_blocking_rule_policy(make_turn, accounts):
    t = make_turn("CUST-2006")  # vulnerable -> blocking rule is POL-CON-004
    t.raw_account = accounts["CUST-2006"]
    t.account_state, t.account_attempted = redact_account(t.raw_account), True
    t.facts = compute_facts(t.raw_account)
    t.eligibility = evaluate(t.raw_account, t.facts)
    t.retrieved[("POL-GOV-009", "body")] = {"doc_id": "POL-GOV-009", "section": "body", "text": "Approved reason codes"}
    payload = {
        "status": "declined", "language": "en",
        "claims": [{"id": "c1", "type": "gap", "text": "We can't increase your limit, and no approved reason code applies.",
                    "citations": [{"kind": "policy", "doc_id": "POL-GOV-009", "section": "body", "quote": None}]}],
        "decision": {"outcome": "declined", "reason_code": None}, "routing": None,
        "unanswered": ["approved reason code"], "conflicts_noted": [],
    }
    _, errors = validate(payload, t)
    assert any("POL-CON-004" in e for e in errors)

    # D4 gap evidenced by the eligibility tool (no 'unanswered' needed) + the rule doc cited -> valid
    t.retrieved[("POL-CON-004", "body")] = {"doc_id": "POL-CON-004", "section": "body", "text": "Vulnerable Customer Policy"}
    payload["unanswered"] = []
    payload["claims"][0]["citations"] = [{"kind": "tool", "tool": "check_limit_increase_eligibility", "request_id": None},
                                         {"kind": "policy", "doc_id": "POL-CON-004", "section": "body", "quote": None}]
    _, errors = validate(payload, t)
    assert errors == []


def test_string_null_reason_code_is_normalised(turn):
    payload, errors = validate(_with(decision={"outcome": "none", "reason_code": "null"}), turn)
    assert errors == [] and payload.decision.reason_code is None


def test_score_request_must_offer_reason_codes(turn):
    from src.modules.guardrails.input_guard import check

    turn.input_flags = check("What is my exact CIBIL score?", "CUST-2000")["flags"]
    assert turn.input_flags == ["internal_score_request"]
    _, errors = validate(GOOD, turn)
    assert any("POL-GOV-009" in e for e in errors)


def test_decline_needs_declined_status(turn):
    p = _with(decision={"outcome": "declined", "reason_code": None}, status="answered_with_gap")
    _, errors = validate(p, turn)
    assert any("must go together" in e for e in errors)


def test_rejects_internal_score_field(turn):
    _, errors = validate(_with(claims__1__citations=[{"kind": "account", "field": "behavioural_score"}]), turn)
    assert any("behavioural_score" in e for e in errors)


def test_rejects_schema_violation(turn):
    _, errors = validate(_with(status="approved"), turn)
    assert errors and errors[0].startswith("schema")


def test_rejects_reason_code_not_supported_by_rules(make_turn, accounts):
    t = make_turn("CUST-2007")  # low utilisation only -> no approved code applies (D4)
    t.raw_account = accounts["CUST-2007"]
    t.account_state, t.account_attempted = redact_account(t.raw_account), True
    t.facts = compute_facts(t.raw_account)
    t.eligibility = evaluate(t.raw_account, t.facts)
    payload = {
        "status": "declined", "language": "en",
        "claims": [{"id": "c1", "type": "decision", "text": "We can't increase your limit right now.",
                    "citations": [{"kind": "tool", "tool": "check_limit_increase_eligibility", "request_id": None}]}],
        "decision": {"outcome": "declined", "reason_code": "AA06"},
        "routing": None, "unanswered": [], "conflicts_noted": [],
    }
    _, errors = validate(payload, t)
    assert any("AA06" in e for e in errors)


def test_rejects_unknown_service_request(turn):
    p = _with(status="request_raised", routing={"queue": "DISPUTES", "request_id": "SR-DEADBEEF", "sla": None})
    _, errors = validate(p, turn)
    assert any("raise_service_request" in e for e in errors)
