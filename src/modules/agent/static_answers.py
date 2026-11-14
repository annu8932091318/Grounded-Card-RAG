"""EXIT rules for static mode (every LLM is down). With no model to read the question, code must be strict:

0. Flagged questions (injection, another customer, internal score) -> refusal + human handoff, nothing else.
1. Account lookups are answered deterministically from the customer's own record (cited account fields):
   "When is my payment due?" -> due date + minimum due + statement balance. No LLM, no guessing, no policy dump.
   Only clear "what is my / when is my" lookups qualify; fees, late payment, why, increases, changes are NOT matched.
1b. Action requests (raise my limit, block my card, send a statement) -> no excerpt: static mode can't act, and loosely
   related policy lines read like an answer. Honest "can't do that right now" + human handoff.
2. A policy excerpt is shown only if it actually addresses the question:
   * only the lines (table rows / sentences) that contain the question's key terms are shown — never a whole section
   * and those lines must cover >= 2 of 3 of the question's key terms (terms unknown to the whole corpus excluded)
   Otherwise no excerpt at all: an honest "can't answer right now" + a human handoff beats irrelevant text.
   Found live: "When is my payment due?" matched "Late payment" in the fee table, so the whole table was shown.
"""
import re
from datetime import date

from src.helper.money import format_inr
from src.modules.retrieval.keyword_search import GENERIC_TERMS, terms

STATIC_EXCERPT_MIN_COVERAGE = 0.66  # at least 2 of 3 key terms; with 1-2 key terms: all of them

# Words that carry no topic (on top of the BM25 stopwords): question framing, filler, and time words
# ("send me last month's statement" must not match "past due in the last 12 months").
_QUESTION_WORDS = {"much", "many", "long", "use", "using", "used", "tell", "know", "need", "want", "like", "would",
                   "check", "please", "kindly", "help", "anything", "something", "about", "regarding", "explain", "show",
                   "give", "does", "doing", "also", "still", "now", "today", "last", "next", "month", "year", "week",
                   "day", "time", "ago"}

# Input flags that mean static mode must neither look anything up nor show policy text: refuse + route to a human.
REFUSE_FLAGS = {"injection_suspected", "cross_customer_request", "internal_score_request"}

# Action requests: static mode cannot DO anything (raise a limit, block a card, send a statement, change a payment),
# so no excerpt is shown — showing loosely related policy lines reads like an answer. Stems catch "increased",
# "raising", "changed". Information questions ("can I still dispute?", "what is the interest rate?") are not actions.
_ACTION = re.compile(r"\b(increas\w*|rais\w*|higher|lower\w*|reduc\w*|block\w*|lost|stolen|chang\w*|updat\w*|"
                     r"send\w*|cancel\w*|close|closing|complain\w*|replace\w*|reissu\w*|transfer\w*)\b", re.I)

# A lookup must ask for the value itself; anything about fees, missing a payment, or "why" is a policy question.
_NOT_A_LOOKUP = re.compile(r"\b(why|how come|fee|fees|charge\w*|late|miss\w*|penalt\w*|interest|wrong|dispute\w*|"
                           r"waiv\w*|revers\w*|refund\w*|can i|could i|should i|if i)\b", re.I)

ACCOUNT_INTENTS = [
    # (name, pattern, account fields cited) — each pattern asks FOR the value ("when is…", "what is my…")
    ("due_date", re.compile(r"\b(when (is|was) my (payment|bill|card payment|statement)( \w+)? due|"
                            r"when (do|should|must) i pay|what('s| is) my (payment )?due date|my due date\b|"
                            r"last date to pay|pay by when)", re.I), ["due_date", "minimum_due", "statement_balance"]),
    ("minimum_due", re.compile(r"\b(what('s| is) (my|the) minimum (amount )?(due|payment)|minimum (amount )?due on my)", re.I),
     ["minimum_due", "due_date"]),
    ("balance", re.compile(r"\b(what('s| is) my (statement |current |outstanding )?balance|how much (do|did) i owe|"
                           r"my (statement|outstanding) balance)", re.I), ["statement_balance", "minimum_due", "due_date"]),
    ("current_limit", re.compile(r"\b(what('s| is) my (current |credit |card )*limit|my current (credit )?limit)", re.I),
     ["current_limit"]),
]


def is_action_request(question: str) -> bool:
    return bool(_ACTION.search(question))


def _stem(word: str) -> str:
    """fees -> fee, charges -> charge, payments -> payment (enough for matching question terms to policy lines)."""
    for suffix in ("ies", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def key_terms(question_norm: str) -> set[str]:
    return {_stem(t) for t in terms(question_norm) if len(t) >= 3} - {_stem(g) for g in GENERIC_TERMS} - _QUESTION_WORDS


def _words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower())}


# --- 1. deterministic account lookups ----------------------------------------------------------------------------

def match_account_intent(question: str) -> tuple[str, list[str]] | None:
    if _NOT_A_LOOKUP.search(question) or _ACTION.search(question):
        return None
    for name, pattern, fields in ACCOUNT_INTENTS:
        if pattern.search(question):
            return name, fields
    return None


def _date(value: str) -> str:
    d = date.fromisoformat(value)
    return f"{d.day} {d:%B %Y}"


def render_account_answer(intent: str, account: dict) -> str | None:
    """Plain sentences built only from the record's own values (amounts in Indian grouping, dates spelled out)."""
    try:
        if intent == "due_date":
            return (f"Your payment due date is {_date(account['due_date'])}. The minimum amount due is "
                    f"{format_inr(account['minimum_due'])}, on a statement balance of {format_inr(account['statement_balance'])}.")
        if intent == "minimum_due":
            return (f"The minimum amount due on your card is {format_inr(account['minimum_due'])}, "
                    f"payable by {_date(account['due_date'])}.")
        if intent == "balance":
            return (f"Your statement balance is {format_inr(account['statement_balance'])}. The minimum amount due is "
                    f"{format_inr(account['minimum_due'])}, payable by {_date(account['due_date'])}.")
        if intent == "current_limit":
            return f"Your current credit limit is {format_inr(account['current_limit'])}."
    except (KeyError, TypeError, ValueError):
        return None  # a field is missing / malformed: say nothing rather than something wrong
    return None


# --- 2. relevant policy lines only ------------------------------------------------------------------------------

def relevant_excerpt(question_norm: str, chunk_text: str, vocabulary: set[str] | None = None) -> str | None:
    """The best-matching lines of the chunk for the question's key terms, or None if they don't cover enough of them.
    * Terms that appear nowhere in the corpus (`vocabulary`), like "Singapore", are context no policy can match, so
      they don't count against coverage. Terms the corpus knows (like "due") must be present.
    * Only the lines matching the MOST key terms are kept ("late payment fee" -> the late-payment rows, not every
      row that says "fee"); a table keeps its header row."""
    wanted = key_terms(question_norm)
    if vocabulary is not None:
        wanted = {t for t in wanted if t in vocabulary}
    if not wanted:
        return None
    body = chunk_text.split(": ", 1)[-1].strip()  # drop the "POL-XXX Title §section:" prefix
    lines = [ln.strip() for ln in re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z])", body) if ln.strip()]
    scored = [(len(wanted & _words(ln)), ln) for ln in lines if not re.fullmatch(r"\|?[\s|:-]+\|?", ln)]
    best = max((n for n, _ in scored), default=0)
    if best == 0:
        return None
    keep = [ln for n, ln in scored if n == best]
    covered = set().union(*(wanted & _words(ln) for ln in keep))
    if len(covered) / len(wanted) < STATIC_EXCERPT_MIN_COVERAGE:
        return None
    if any(ln.startswith("|") for ln in keep):
        header = next(ln for ln in lines if ln.startswith("|"))
        if header not in keep:
            keep.insert(0, header)  # a table row needs its header to be readable
    return "\n".join(keep)


def corpus_vocabulary(tenant: str = "default") -> set[str] | None:
    """Every (stemmed) word in the searchable corpus, from the BM25 index. None if the index can't be loaded."""
    try:
        from src.modules.retrieval import keyword_search

        return {_stem(t) for t in keyword_search._get_index(tenant)["df"]}
    except Exception:
        return None
