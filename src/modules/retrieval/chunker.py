"""Parse a policy markdown file (header metadata) and split it into section chunks."""
import re
from datetime import datetime

from src.helper.text import normalise

_TITLE = re.compile(r"^#\s+(.+)$", re.M)
_HEADER = re.compile(r"^Document ID:.*$", re.M)
_SECTION_NUM = re.compile(r"^(\d+)\.\s")


def parse_doc(raw: str) -> dict:
    """-> {doc_id, title, version, effective_date, owner, body, raw}"""
    title = _TITLE.search(raw).group(1).strip()
    header_line = _HEADER.search(raw).group(0)
    meta = {"doc_id": None, "version": None, "effective_date": None, "owner": None}
    for part in (p.strip() for p in header_line.split("|")):
        if part.startswith("Document ID:"):
            meta["doc_id"] = part.split(":", 1)[1].strip()
        elif part.startswith("Version"):
            meta["version"] = part[len("Version"):].strip()  # '4.2' or '0.9 DRAFT'
        elif part.startswith("Effective"):
            meta["effective_date"] = datetime.strptime(part[len("Effective"):].strip(), "%d-%b-%Y").date()
        elif part.startswith("Owner:"):
            meta["owner"] = part.split(":", 1)[1].strip()
    body = raw[raw.index(header_line) + len(header_line):].strip()
    return {**meta, "title": title, "body": body, "raw": raw}


def _section_id(heading: str) -> str:
    """'1. Eligibility' -> '1' ; 'Classic Card' -> 'Classic Card'"""
    m = _SECTION_NUM.match(heading)
    return m.group(1) if m else heading.strip()


def chunk_doc(doc: dict) -> list[dict]:
    """Split on '## ' headings. No headings -> one chunk ('Table' if it holds the fee table, else 'body').
    WHY whole-doc chunk for the fee table: a split table loses its header row and rows lose meaning."""
    parts = re.split(r"^## ", doc["body"], flags=re.M)
    pieces: list[tuple[str, str]] = []
    if len(parts) == 1:
        section = "Table" if re.search(r"^\|", doc["body"], re.M) else "body"
        pieces.append((section, doc["body"]))
    else:
        if parts[0].strip():  # preamble before the first heading
            pieces.append(("intro", parts[0]))
        for part in parts[1:]:
            heading, _, content = part.partition("\n")
            pieces.append((_section_id(heading), f"{heading.strip()}\n{content}"))

    chunks = []
    for section, content in pieces:
        text = f"{doc['doc_id']} {doc['title']} §{section}: {content.strip()}"
        chunks.append({
            "section": section,
            "text": text,
            "text_norm": normalise(text),
            "token_count": len(text.split()),  # WHY: word count is a good-enough proxy at this size
        })
    return chunks
