"""Derived facts computed in code from the raw account record (dates pinned to AS_OF_DATE).
Every constant below is copied from the corpus; tests/test_policy_drift.py asserts each literal still appears there."""
from src.helper.dates import add_months, add_years, as_of, days_overdue, months_between, parse

# --- policy constants (doc + literal in comments) ---
COOLDOWN_MONTHS = 6            # POL-CRD-001 §3 "preceding 6 months" (POL-CRD-011's 3 months loses — D1)
THIN_FILE_MONTHS = 6           # POL-CRD-003 §5 "fewer than 6 months"
MIN_UTILISATION = 0.40         # POL-CRD-001 §1 "at least 40%"
MIN_BEHAVIOURAL_SCORE = 620    # POL-CRD-001 §1 "620 or above" — internal, never disclosed
MAX_INCREASE_MULTIPLE = 2.0    # POL-CRD-001 §2 "2.0x the existing limit"
THIN_FILE_CAP_MULTIPLE = 1.25  # POL-CRD-003 §5 "1.25x"
KYC_REFRESH_YEARS = {"high": 2, "medium": 8, "low": 10}  # POL-KYC-008
KYC_OVERDUE_BAR_DAYS = 90      # POL-KYC-008 "more than 90 days"
FEE_WAIVER_SPEND = {"Classic": 150_000, "Signature": 400_000}  # POL-FEE-007 "1,50,000" / "4,00,000"
LATE_FEE_BALANCE_BAND = 10_000  # POL-FEE-007 "10,000"
LATE_FEE_LOW, LATE_FEE_HIGH = 500, 1_000  # POL-FEE-007

# D8: these inputs are required by POL-CRD-001 §1-3 / POL-CRD-002 but do not exist in account state.
MISSING_DATA = ["assessed monthly net income", "payment delinquency history (30+ days past due)",
                "debt-burden ratio after increase"]


def compute_facts(raw: dict) -> dict:
    today = as_of()
    last_inc = parse(raw.get("last_limit_increase_date"))
    kyc_due = add_years(parse(raw["kyc_last_done"]), KYC_REFRESH_YEARS[raw["kyc_risk_band"]])
    kyc_overdue = max(0, days_overdue(kyc_due, today))
    product = raw["card_product"]
    threshold = FEE_WAIVER_SPEND[product]
    limit = raw["current_limit"]
    return {
        "as_of_date": today.isoformat(),
        "card_product": product,
        "current_limit": limit,
        "months_since_last_increase": months_between(last_inc, today) if last_inc else None,
        "last_increase_within_6_months": bool(last_inc and add_months(last_inc, COOLDOWN_MONTHS) > today),
        "next_increase_possible_from": add_months(last_inc, COOLDOWN_MONTHS).isoformat() if last_inc else None,
        "kyc_due_date": kyc_due.isoformat(),
        "kyc_overdue_days": kyc_overdue,
        "kyc_overdue_more_than_90_days": kyc_overdue > KYC_OVERDUE_BAR_DAYS,
        # Thresholds named in fact keys are also facts, so a claim using them is number-grounded (EV-021).
        "kyc_overdue_bar_days": KYC_OVERDUE_BAR_DAYS,
        "limit_increase_cooldown_months": COOLDOWN_MONTHS,
        # WHY internal history only: bureau trade lines are not in the data (D8) — declared assumption.
        "thin_file": raw["months_repayment_history"] < THIN_FILE_MONTHS,
        "months_repayment_history": raw["months_repayment_history"],
        "utilisation_pct": round(raw["avg_utilisation_3m"] * 100),
        "has_open_dispute": bool(raw["open_dispute"]),
        "is_vulnerable": bool(raw["vulnerable_flag"]),
        "card_year_spend": raw["card_year_spend"],
        "annual_fee_waiver_threshold": threshold,
        "annual_fee_waived_by_spend": raw["card_year_spend"] >= threshold,
        "late_fee_if_payment_missed": LATE_FEE_HIGH if raw["statement_balance"] > LATE_FEE_BALANCE_BAND else LATE_FEE_LOW,
        "max_limit_under_2x_rule": int(limit * MAX_INCREASE_MULTIPLE),
        "max_limit_if_thin_file": int(limit * THIN_FILE_CAP_MULTIPLE),
        "data_not_available": MISSING_DATA,
    }
