"""Output guard: score leaks, approval language, sales to vulnerable customers, flag disclosure, other customers."""
from src.modules.agent.respond_schema import RespondPayload
from src.modules.guardrails.output_guard import check


def _payload(*texts: str) -> RespondPayload:
    return RespondPayload.model_validate({
        "status": "answered", "language": "en",
        "claims": [{"id": f"c{i}", "type": "policy_fact", "text": t,
                    "citations": [{"kind": "account", "field": "current_limit"}]} for i, t in enumerate(texts)],
        "decision": {"outcome": "none", "reason_code": None},
    })


def _turn(make_turn, accounts, cid):
    t = make_turn(cid)
    t.raw_account = accounts[cid]
    return t


def test_blocks_internal_score_value(make_turn, accounts):
    t = _turn(make_turn, accounts, "CUST-2000")  # score 640
    assert check(_payload("Your number is 640."), t)
    assert "leak_blocked" in t.trace.flags


def test_blocks_approval_language(make_turn, accounts):
    t = _turn(make_turn, accounts, "CUST-2000")
    assert check(_payload("Your limit increase has been approved."), t)


def test_allows_negated_approval(make_turn, accounts):
    t = _turn(make_turn, accounts, "CUST-2000")
    assert check(_payload("I can't approve a limit increase; I have referred it to our credit team."), t) == []


def test_allows_policy_description_of_past_approvals(make_turn, accounts):
    """Regression (eval EV-024): describing the 6-month rule is not us approving anything."""
    t = _turn(make_turn, accounts, "CUST-2001")
    assert check(_payload("Requests are declined if a limit increase has been approved in the preceding 6 months."), t) == []


def test_naming_an_approved_reason_code_is_not_approval(make_turn, accounts):
    """Regression (eval EV-004)."""
    t = _turn(make_turn, accounts, "CUST-2001")
    assert check(_payload("The approved reason code for this limit increase decision is AA05."), t) == []


def test_blocks_sales_and_flag_talk_for_vulnerable(make_turn, accounts):
    t = _turn(make_turn, accounts, "CUST-2006")
    v = check(_payload("You could upgrade to a premium card.", "Your account is flagged as vulnerable."), t)
    assert any("sales_to_vulnerable" in x for x in v) and any("flag_disclosed" in x for x in v)


def test_blocks_other_customer_reference(make_turn, accounts):
    t = _turn(make_turn, accounts, "CUST-2000")
    assert check(_payload("CUST-2003 has a limit of 2,50,000."), t)
