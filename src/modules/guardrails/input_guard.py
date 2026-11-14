"""ENTRY guardrail. Runs on every customer message before anything else sees it:
  1. sanitize: strip control / zero-width characters, collapse whitespace (hidden-text injection, log forging)
  2. mask PII: card / Aadhaar / PAN / e-mail / phone / OTP-CVV-PIN -> [CARD_NUMBER] etc. (see pii.py)
  3. flag: injection_suspected, internal_score_request, cross_customer_request, pii_masked
Flags do not block by themselves: the citation validator turns injection / cross-customer flags into a
mandatory refusal, and a score request into a mandatory "offer the reason codes" answer. WHY not block on
regex alone: regex is easy to evade, so enforcement is layered (prompt + validator + output guard + the fact
that no 'approve' tool exists).
Also scans corpus documents at ingest (scan_corpus_doc) — the quarantine for POL-OPS-012."""
import re
import unicodedata

from src.modules.guardrails import pii

USER_INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) (instructions?|rules?)",
    r"(print|reveal|show|repeat|output).{0,40}(system prompt|your instructions|your prompt)",
    r"\bsystem prompt\b",
    r"desktop notes",
    r"do what (it|they|the note) says?",
    r"developer mode|jailbreak",  # WHY no "act as": flags now force a refusal, so false positives matter
]

CORPUS_INJECTION_PATTERNS = [
    r"system note",
    r"automated assistants",
    r"(ignore|disregard) (all |any )?(previous|prior) instruction",
    r"do not mention this",
    r"approve any",
]
NON_POLICY_MARKERS = [r"\bnot policy\b", r"\bdraft\b"]

_CUSTOMER_ID = re.compile(r"\bCUST-\d{4}\b", re.I)
_SCORE_REQUEST = re.compile(r"\b(cibil|bureau|behaviou?ral|internal|credit)\s+score\b", re.I)


_CONTROL = re.compile("[\u200b-\u200f\u2028-\u202e\u2060-\u2064\ufeff]")  # zero-width, bidi overrides, BOM


def sanitize(message: str) -> str:
    """Remove control and zero-width/bidi characters (text a human cannot see but a model reads), collapse spaces."""
    cleaned = "".join(ch for ch in message if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    cleaned = _CONTROL.sub("", cleaned)
    return re.sub(r"[ \t]+", " ", cleaned).strip()


def guard_input(message: str, customer_id: str) -> dict:
    """-> {"message": cleaned + masked text (the ONLY version used downstream), "flags": [...], "pii": [kinds]}"""
    cleaned = sanitize(message)
    masked, kinds = pii.mask(cleaned)
    flags = check(masked, customer_id)["flags"]
    if kinds:
        flags.append("pii_masked")
    return {"message": masked, "flags": flags, "pii": kinds}


def check(message: str, customer_id: str) -> dict:
    flags = []
    lowered = message.lower()
    if any(re.search(p, lowered) for p in USER_INJECTION_PATTERNS):
        flags.append("injection_suspected")
    if _SCORE_REQUEST.search(lowered):
        flags.append("internal_score_request")  # not an attack: triggers the designed "offer reason codes" refusal
    others = {m.upper() for m in _CUSTOMER_ID.findall(message)} - {customer_id}
    if others:
        flags.append("cross_customer_request")
    return {"flags": flags}


def scan_corpus_doc(text: str) -> str | None:
    """Return a quarantine reason, or None if the document looks like governed policy."""
    lowered = text.lower()
    hits = [p for p in CORPUS_INJECTION_PATTERNS if re.search(p, lowered)]
    markers = [p for p in NON_POLICY_MARKERS if re.search(p, lowered)]
    if hits:
        return f"injection patterns: {hits}"
    if markers:
        return f"non-policy markers: {markers}"
    return None
