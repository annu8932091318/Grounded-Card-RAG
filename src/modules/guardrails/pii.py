"""PII patterns shared by the entry guard (mask what the customer typed before it reaches the LLM, retrieval,
Postgres or the log file) and the exit guard (never send PII back out).

Masked kinds: card numbers (13-19 digits), Aadhaar (12 digits), PAN, e-mail, Indian mobile numbers, and secrets
typed after OTP / CVV / PIN / password. None of them is needed to answer a policy or account question:
the customer is already identified by the session (customer_id is bound server-side).
Limitation: regex, so it masks formats, not meaning (a card number spelled out in words is not caught).
"""
import re

PATTERNS = [
    # order matters: secrets first (so "OTP 123456" is not left as a bare number), then the long digit runs
    ("secret", re.compile(r"\b(otp|cvv|cvc|pin|m-?pin|password|passcode)\b(\W{0,3}(is|:|=)?\W{0,3})\S{3,20}", re.I)),
    ("card_number", re.compile(r"(?<![\w-])(?:\d[ -]?){12,18}\d(?![\w-])")),
    ("aadhaar", re.compile(r"(?<![\w-])\d{4}[ -]?\d{4}[ -]?\d{4}(?![\w-])")),
    ("pan", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("phone", re.compile(r"(?<![\w-])(?:\+?91[ -]?)?[6-9]\d{9}(?![\w-])")),
]


def find(text: str) -> list[str]:
    """Kinds of PII present in text (deduplicated, in pattern order)."""
    return [kind for kind, rx in PATTERNS if rx.search(text)]


def mask(text: str) -> tuple[str, list[str]]:
    """-> (masked text, kinds found). 'my card 4111 1111 1111 1111' -> 'my card [CARD_NUMBER]'"""
    kinds = []
    for kind, rx in PATTERNS:
        if kind == "secret":
            new = rx.sub(lambda m: f"{m.group(1)} [SECRET]", text)
        else:
            new = rx.sub(f"[{kind.upper()}]", text)
        if new != text:
            kinds.append(kind)
            text = new
    return text, kinds
