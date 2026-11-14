"""Flags (does not block) suspicious user input, and scans corpus docs for injected instructions at ingest.
WHY flag-only for users: regex is easy to evade; enforcement is the prompt + validator + output guard +
the fact that no 'approve' tool exists."""
import re

USER_INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) (instructions?|rules?)",
    r"(print|reveal|show|repeat|output).{0,40}(system prompt|your instructions|your prompt)",
    r"\bsystem prompt\b",
    r"desktop notes",
    r"do what (it|they|the note) says?",
    r"developer mode|jailbreak|act as",
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


def check(message: str, customer_id: str) -> dict:
    flags = []
    lowered = message.lower()
    if any(re.search(p, lowered) for p in USER_INJECTION_PATTERNS):
        flags.append("injection_suspected")
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
