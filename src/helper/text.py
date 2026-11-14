"""Text + number normalisation shared by FTS indexing, querying and the citation validator."""
import re

from src.helper.money import words_to_numbers

_DIGIT_COMMA = re.compile(r"(?<=\d),(?=\d)")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
# IDs contain digits that are not "facts" (POL-CRD-001, CUST-2003, SR-1A2B3C4D, AA04, §3 handled separately)
_IDS = re.compile(r"\b(?:POL-[A-Z]{3}-\d{3}|CUST-\d{4}|SR-[0-9A-Fa-f]{8}|AA0\d)\b")
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])")
_WS = re.compile(r"\s+")


def normalise(text: str) -> str:
    """1,50,000 -> 150000 ; 3.5% -> 3.5 percent ; 3 lakh -> 300000 ; collapse whitespace; lowercase."""
    t = _DIGIT_COMMA.sub("", text)
    t = words_to_numbers(t)
    t = _PERCENT.sub(r"\1 percent", t)
    t = t.replace("₹", " ").replace("Rs.", " ").replace("INR", " ")
    return _WS.sub(" ", t).strip().lower()


def canonical_number(n: str) -> str:
    """'2.0' -> '2', '3.50' -> '3.5', '0400' -> '400' so equal values compare equal."""
    f = float(n)
    return str(int(f)) if f == int(f) else f"{f:g}"


def extract_numbers(text: str) -> set[str]:
    """Every numeric value in text (IDs removed), canonicalised. Used for number grounding."""
    t = _IDS.sub(" ", text)
    t = _DIGIT_COMMA.sub("", t)
    t = words_to_numbers(t)
    t = t.replace("-", " ")  # WHY: dates like 2026-09-08 -> 2026 09 08, compared as numbers
    return {canonical_number(n) for n in _NUMBER.findall(t)}
