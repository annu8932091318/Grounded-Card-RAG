"""Last check before the customer sees anything: score leaks, approval language, sales to vulnerable
customers, prompt leaks, non-policy citations, other customers' ids. Returns a list of violations."""
import re

from src.helper.prompt_loader import load_prompt
from src.helper.text import extract_numbers

_NEGATION = re.compile(r"\b(not|cannot|can't|can not|unable|never|no|won't)\b", re.I)
# WHY not "increased": "your limit was increased on 2026-06-19" is a true account fact (EV-004).
_APPROVAL = re.compile(r"\b(approved|actioned|processed|granted)\b", re.I)
_LIMIT = re.compile(r"\b(limit|increase)\b", re.I)
_SCORE_TALK = re.compile(r"\b(behaviou?ral|internal|cibil|credit|bureau)\s+score\b[^.]{0,30}\d", re.I)
_SALES = re.compile(r"\b(offers?|upgrades?|pre-?approved|promotions?|cross-?sell|special deal)\b", re.I)
_FLAG_TALK = re.compile(r"\b(vulnerab\w*|flagged|flag)\b", re.I)
_CUSTOMER_ID = re.compile(r"\bCUST-\d{4}\b", re.I)


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _prompt_fragments() -> list[str]:
    """Distinctive sentences (>= 8 words) of the active system prompt, lowercased."""
    content = load_prompt()["content"].lower()
    return [s.strip() for s in re.split(r"[.\n]", content) if len(s.split()) >= 8]


def check(payload, turn) -> list[str]:
    texts = [c.text for c in payload.claims]
    full = " ".join(texts)
    violations = []
    raw = turn.raw_account

    if raw and str(raw["behavioural_score"]) in extract_numbers(full):
        violations.append("internal_score_leak: the internal score value must never appear")
    if _SCORE_TALK.search(full):
        violations.append("internal_score_leak: never state any score value")
    for s in _sentences(full):
        if _APPROVAL.search(s) and _LIMIT.search(s) and not _NEGATION.search(s):
            violations.append(f"approval_language: '{s[:80]}' — this service cannot approve or action limit changes")
        if raw and raw.get("vulnerable_flag") and _SALES.search(s) and not _NEGATION.search(s):
            violations.append("sales_to_vulnerable: no offers/upgrades/sales content for this customer")
    if raw and raw.get("vulnerable_flag") and _FLAG_TALK.search(full):
        violations.append("flag_disclosed: do not tell the customer about any flag or vulnerability marker")
    lowered = full.lower()
    if any(f in lowered for f in _prompt_fragments()):
        violations.append("prompt_leak: system prompt text must never be output")
    if any(c.kind == "policy" and c.doc_id == "POL-OPS-012" for cl in payload.claims for c in cl.citations):
        violations.append("non_policy_cited: POL-OPS-012 is not policy")
    others = {m.upper() for m in _CUSTOMER_ID.findall(full)} - {turn.customer_id}
    if others:
        violations.append("other_customer_reference: never mention another customer")
    if violations:
        turn.trace.flag("leak_blocked")
    return violations
