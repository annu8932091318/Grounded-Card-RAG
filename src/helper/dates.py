"""All date arithmetic lives here, pinned to AS_OF_DATE. The LLM never does date maths."""
import calendar
from datetime import date

from src.const.config import config


def as_of() -> date:
    return date.fromisoformat(config.as_of_date)


def parse(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    day = min(d.day, calendar.monthrange(year, month)[1])  # WHY: 31 Jan + 1 month -> 28/29 Feb
    return date(year, month, day)


def add_years(d: date, years: int) -> date:
    return add_months(d, 12 * years)


def months_between(start: date, end: date) -> float:
    """Approximate months, 1 decimal — for display only. Rules use add_months() for exact bars."""
    return round((end - start).days / 30.4375, 1)


def days_overdue(due: date, today: date | None = None) -> int:
    """Positive = overdue by N days; 0 or negative = not overdue."""
    return ((today or as_of()) - due).days
