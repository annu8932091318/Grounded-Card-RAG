"""Policy-as-code checks for a limit-increase request. Rules give *facts*; the corpus gives *authority* —
the LLM must still retrieve and cite the matching policy chunk. There is no 'approve' outcome by design."""
from src.modules.decision.facts import MIN_BEHAVIOURAL_SCORE, MIN_UTILISATION, MISSING_DATA

# Order = which code is given first when several apply (mandatory §3 declines before the rest).
_CODE_PRIORITY = ["AA04", "AA05", "AA06", "AA01", "AA02", "AA03"]


def _rule(name, blocking, doc_id, section, reason_code, explanation):
    return {"rule": name, "blocking": blocking, "doc_id": doc_id, "section": section,
            "reason_code": reason_code, "explanation": explanation}


def evaluate(raw: dict, facts: dict) -> dict:
    """raw = unredacted account (needed for the internal score check, which never leaves this function)."""
    rules = []
    if facts["has_open_dispute"]:
        rules.append(_rule("open_dispute", True, "POL-CRD-001", "3", "AA04",
                           "An unresolved dispute exists on the account (mandatory decline)."))
    if facts["is_vulnerable"]:
        # D4: no approved reason code exists. Decline in neutral words; never tell the customer they are flagged.
        rules.append(_rule("not_eligible_conduct", True, "POL-CON-004", "body", None,
                           "Limit increases are not available on this account at present. No approved reason "
                           "code applies; use neutral wording, do not mention any flag, no sales content."))
    if facts["last_increase_within_6_months"]:
        rules.append(_rule("recent_increase", True, "POL-CRD-001", "3", "AA05",
                           f"Last increase was {facts['months_since_last_increase']} months ago; the bar is 6 months "
                           f"(POL-CRD-001 prevails over POL-CRD-011). Possible again from {facts['next_increase_possible_from']}."))
    if facts["kyc_overdue_more_than_90_days"]:
        rules.append(_rule("kyc_overdue", True, "POL-KYC-008", "body", "AA06",
                           "Re-KYC is overdue by more than 90 days: limits may not be increased; the account is not frozen on this basis alone."))

    if facts["thin_file"]:
        # D5: specific (POL-CRD-003) beats general (POL-CRD-001 §1-2): thin-file customers stay eligible,
        # so the §1 eligibility criteria below are not applied as blockers; cap 1.25x and refer.
        rules.append(_rule("thin_file_cap", False, "POL-CRD-003", "5", "AA03",
                           f"Thin file: any increase is capped at 1.25x (max {facts['max_limit_if_thin_file']}) "
                           "and must be referred to a credit officer, not auto-decisioned."))
    else:
        if raw["avg_utilisation_3m"] < MIN_UTILISATION:
            rules.append(_rule("low_utilisation", True, "POL-CRD-001", "1", None,
                               f"Average utilisation is {facts['utilisation_pct']}%, below the 40% eligibility criterion. "
                               "No approved reason code covers this (D4): decline in plain language and flag the gap."))
        if raw["behavioural_score"] < MIN_BEHAVIOURAL_SCORE:
            rules.append(_rule("internal_criteria_not_met", True, "POL-CRD-001", "1", None,
                               "An internal eligibility criterion is not met. Never disclose or describe it; "
                               "no approved reason code applies truthfully."))

    rules.append(_rule("missing_data", False, "POL-CRD-001", "1", None,
                       "Cannot determine full eligibility: " + "; ".join(MISSING_DATA) +
                       " are not available. Never approve; refer to CREDIT_OPS if not declined."))

    blocking = [r for r in rules if r["blocking"]]
    codes = [r["reason_code"] for r in rules if r["reason_code"]]
    primary = next((c for c in _CODE_PRIORITY if c in [r["reason_code"] for r in blocking]), None)
    return {
        "outcome": "declined" if blocking else "referred",
        "reason_code": primary,
        "applicable_reason_codes": sorted(set(codes)),
        "no_approved_reason_code": bool(blocking) and primary is None,
        # WHY in the tool output: the model kept citing an invented retrieval_miss for this gap (eval run 6).
        "how_to_cite": ("decline claims cite the policy doc of a blocking rule (search_policy for it first); "
                        "a 'no approved reason code applies' gap claim cites this tool, check_limit_increase_eligibility"),
        "refer_to_queue": None if blocking else "CREDIT_OPS",
        "this_service_can_approve": False,
        "rules": rules,
    }
