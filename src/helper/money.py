"""Indian number formats: 1,50,000 <-> 150000 and '3 lakh' -> 300000."""
import re

_UNITS = {"lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "crore": 10_000_000, "crores": 10_000_000, "k": 1_000}
_WORD_AMOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*(lakhs?|lac|crores?|k)\b", re.IGNORECASE)


def parse_amount(text: str) -> float | None:
    """'1,50,000' -> 150000, '3 lakh' -> 300000, '2.5' -> 2.5; None if no number."""
    m = _WORD_AMOUNT.search(text)
    if m:
        return float(m.group(1)) * _UNITS[m.group(2).lower()]
    m = re.search(r"\d[\d,]*(?:\.\d+)?", text)
    return float(m.group(0).replace(",", "")) if m else None


def words_to_numbers(text: str) -> str:
    """Replace '3 lakh' style amounts inside text with plain integers."""
    return _WORD_AMOUNT.sub(lambda m: str(int(float(m.group(1)) * _UNITS[m.group(2).lower()])), text)


def format_inr(amount: float) -> str:
    """150000 -> '1,50,000' (Indian grouping: last 3 digits, then pairs)."""
    whole = str(int(round(amount)))
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    pairs = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    return ",".join([head] + pairs + [tail])
