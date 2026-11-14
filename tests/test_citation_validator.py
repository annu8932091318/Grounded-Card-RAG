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


def test_rejects_non_verbatim_quote(turn):
    bad = [{"kind": "policy", "doc_id": "POL-FEE-007", "section": "Table", "quote": "markup is waived for Signature"}]
    _, errors = validate(_with(claims__0__citations=bad), turn)
    assert any("not verbatim" in e for e in errors)


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
