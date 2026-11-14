"""Every constant hardcoded in const/ and decision/ must still appear (as a literal) in the corpus.
If a policy document changes a number, this test fails before the code silently disagrees with policy.
Limitation: guards literals, not semantics."""
import pytest

from src.const.categories import CATEGORIES
from src.const.config import config
from src.const.reason_codes import REASON_CODES
from src.modules.decision import facts as f

CORPUS = {p.name.split("_")[0]: p.read_text(encoding="utf-8") for p in (config.data_dir / "policy_corpus").glob("*.md")}

LITERALS = [
    (f.COOLDOWN_MONTHS, "POL-CRD-001", "preceding 6 months"),
    (f.THIN_FILE_MONTHS, "POL-CRD-003", "fewer than 6 months"),
    (f.MIN_UTILISATION, "POL-CRD-001", "at least 40%"),
    (f.MIN_BEHAVIOURAL_SCORE, "POL-CRD-001", "620 or above"),
    (f.MAX_INCREASE_MULTIPLE, "POL-CRD-001", "2.0x the existing limit"),
    (f.THIN_FILE_CAP_MULTIPLE, "POL-CRD-003", "1.25x"),
    (f.KYC_REFRESH_YEARS["high"], "POL-KYC-008", "every 2 years for high-risk"),
    (f.KYC_REFRESH_YEARS["medium"], "POL-KYC-008", "8 years for medium-risk"),
    (f.KYC_REFRESH_YEARS["low"], "POL-KYC-008", "10 years for low-risk"),
    (f.KYC_OVERDUE_BAR_DAYS, "POL-KYC-008", "more than 90 days"),
    (f.FEE_WAIVER_SPEND["Classic"], "POL-FEE-007", "Classic | Waived on spend of 1,50,000"),
    (f.FEE_WAIVER_SPEND["Signature"], "POL-FEE-007", "Signature | Waived on spend of 4,00,000"),
    (f.LATE_FEE_BALANCE_BAND, "POL-FEE-007", "balance up to 10,000 | 500"),
    (f.LATE_FEE_HIGH, "POL-FEE-007", "balance above 10,000 | 1,000"),
]


def _number_in_literal(value, literal: str) -> bool:
    plain = literal.replace(",", "")
    as_pct = f"{int(value * 100)}%" if isinstance(value, float) and value < 1 else None
    return str(value) in plain or (as_pct is not None and as_pct in plain) or f"{value:g}" in plain


@pytest.mark.parametrize("value,doc,literal", LITERALS)
def test_constant_literal_in_corpus(value, doc, literal):
    assert literal in CORPUS[doc], f"{doc} no longer contains {literal!r}"
    assert _number_in_literal(value, literal), f"constant {value} does not match literal {literal!r}"


@pytest.mark.parametrize("code,meaning", REASON_CODES.items())
def test_reason_codes_match_gov_009(code, meaning):
    assert f"{code} {meaning}" in " ".join(CORPUS["POL-GOV-009"].split())


@pytest.mark.parametrize("queue", CATEGORIES)
def test_queues_and_slas_match_svc_006(queue):
    text = CORPUS["POL-SVC-006"]
    assert queue in text
    if queue != "GENERAL":
        assert f"{queue}, SLA {CATEGORIES[queue]['sla']}" in text
    else:
        assert "within 1 working day" in text


def test_conflict_d1_still_present():
    assert "3 months after a previous increase" in CORPUS["POL-CRD-011"]
    assert "POL-CRD-001 prevails" in CORPUS["POL-CRD-011"]
