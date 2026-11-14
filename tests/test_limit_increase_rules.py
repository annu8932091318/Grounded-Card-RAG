"""Rules must reproduce the seeded outcomes EV-002..EV-006 (+ EV-020, EV-022) with AS_OF_DATE=2026-11-12."""
import pytest

from src.const.config import config
from src.modules.decision.facts import compute_facts
from src.modules.decision.limit_increase_rules import evaluate


@pytest.fixture(autouse=True)
def pinned_date(monkeypatch):
    monkeypatch.setattr(config, "as_of_date", "2026-11-12")


def _eval(accounts, cid):
    raw = accounts[cid]
    return evaluate(raw, compute_facts(raw))


def test_ev002_open_dispute_is_aa04(accounts):
    r = _eval(accounts, "CUST-2003")
    assert r["outcome"] == "declined" and r["reason_code"] == "AA04"


def test_ev003_vulnerable_declined_without_code(accounts):
    r = _eval(accounts, "CUST-2006")
    assert r["outcome"] == "declined" and r["reason_code"] is None and r["no_approved_reason_code"]


def test_ev004_six_month_bar_prevails_over_three(accounts):
    facts = compute_facts(accounts["CUST-2001"])
    assert facts["months_since_last_increase"] == 4.8  # outside 3 months, inside 6 -> conflict is decisive
    r = evaluate(accounts["CUST-2001"], facts)
    assert r["outcome"] == "declined" and r["reason_code"] == "AA05"


def test_ev005_thin_file_referred_with_cap(accounts):
    r = _eval(accounts, "CUST-2005")
    assert r["outcome"] == "referred" and r["refer_to_queue"] == "CREDIT_OPS"
    assert "AA03" in r["applicable_reason_codes"]
    assert compute_facts(accounts["CUST-2005"])["max_limit_if_thin_file"] == 150000


def test_ev006_kyc_overdue_is_aa06(accounts):
    facts = compute_facts(accounts["CUST-2008"])
    assert facts["kyc_overdue_more_than_90_days"] and facts["kyc_due_date"] == "2018-04-02"
    assert evaluate(accounts["CUST-2008"], facts)["reason_code"] == "AA06"


def test_ev020_missing_data_means_refer_never_approve(accounts):
    r = _eval(accounts, "CUST-2000")
    assert r["outcome"] == "referred" and r["this_service_can_approve"] is False
    assert any(x["rule"] == "missing_data" for x in r["rules"])


def test_ev022_low_utilisation_has_no_code(accounts):
    r = _eval(accounts, "CUST-2007")
    assert r["outcome"] == "declined" and r["reason_code"] is None and r["applicable_reason_codes"] == []


def test_internal_score_never_in_output(accounts):
    r = _eval(accounts, "CUST-2004")  # score 590 < 620
    assert "590" not in str(r) and r["outcome"] == "declined"


def test_fee_and_late_fee_facts(accounts):
    assert compute_facts(accounts["CUST-2001"])["annual_fee_waived_by_spend"] is True   # 4,15,000 >= 4,00,000
    assert compute_facts(accounts["CUST-2000"])["annual_fee_waived_by_spend"] is False  # 1,62,000 <  4,00,000
    assert compute_facts(accounts["CUST-2002"])["late_fee_if_payment_missed"] == 1000
