"""Chunking, quarantine scan, normalisation, keyword query building and RRF (DB-free parts of retrieval)."""
from src.const.config import config
from src.helper.money import format_inr, parse_amount
from src.helper.text import extract_numbers, normalise
from src.modules.guardrails.input_guard import scan_corpus_doc
from src.modules.retrieval.chunker import chunk_doc, parse_doc
from src.modules.retrieval.fuse import rrf
from src.modules.retrieval.keyword_search import to_tsquery

CORPUS = config.data_dir / "policy_corpus"


def _doc(name: str) -> dict:
    return parse_doc((CORPUS / name).read_text(encoding="utf-8"))


def test_header_parsing():
    d = _doc("POL-CRD-001_credit_limit_increase.md")
    assert (d["doc_id"], d["version"], d["owner"]) == ("POL-CRD-001", "4.2", "Credit Policy")
    assert d["effective_date"].isoformat() == "2026-04-01"
    ops = _doc("POL-OPS-012_agent_desktop_notes.md")
    assert ops["version"] == "0.9 DRAFT" and ops["effective_date"] is None


def test_chunk_by_section_keeps_section_ids():
    sections = [c["section"] for c in chunk_doc(_doc("POL-CRD-001_credit_limit_increase.md"))]
    assert sections == ["1", "2", "3", "4"]
    prd = [c["section"] for c in chunk_doc(_doc("POL-PRD-010_product_terms_summary.md"))]
    assert prd == ["Classic Card", "Signature Card", "Personal Loan"]


def test_fee_table_is_one_whole_chunk():
    chunks = chunk_doc(_doc("POL-FEE-007_fees_and_charges.md"))
    assert len(chunks) == 1 and chunks[0]["section"] == "Table"
    assert "Foreign currency markup" in chunks[0]["text"] and "Annual card fee, Classic" in chunks[0]["text"]
    assert chunks[0]["text"].startswith("POL-FEE-007 Fees and Charges Schedule §Table:")


def test_quarantine_scan_flags_only_the_desktop_notes():
    flagged = {p.name for p in CORPUS.glob("*.md") if scan_corpus_doc(p.read_text(encoding="utf-8"))}
    assert flagged == {"POL-OPS-012_agent_desktop_notes.md"}


def test_number_normalisation():
    assert normalise("Waived on spend of 4,00,000") == "waived on spend of 400000"
    assert normalise("Markup 3.5%") == "markup 3.5 percent"
    assert normalise("increase to 3 lakh") == "increase to 300000"
    assert extract_numbers("SR-1A2B3C4D for POL-CRD-001 costs 1,000 and 2.0x") == {"1000", "2"}
    assert parse_amount("2 lakh") == 200000 and format_inr(150000) == "1,50,000"


def test_keyword_query_is_or_of_content_words():
    assert to_tsquery("what is the markup if i use my card in singapore") == "'markup' | 'use' | 'card' | 'singapore'"
    assert to_tsquery("the a of") == ""


def _fake_kw_rows(query: str) -> list[dict]:
    """In-memory stand-in for the FTS leg: chunks containing any query term, most matched terms first."""
    from src.modules.retrieval.keyword_search import terms

    q_terms = set(terms(normalise(query)))
    rows = []
    for p in CORPUS.glob("*.md"):
        raw = p.read_text(encoding="utf-8")
        if scan_corpus_doc(raw):
            continue  # quarantined docs are never searched
        for c in chunk_doc(parse_doc(raw)):
            words = set(c["text_norm"].replace("|", " ").split())
            if q_terms & words:
                rows.append((len(q_terms & words), c))
    return [c for _, c in sorted(rows, key=lambda r: -r[0])]


def test_relevance_gate_keyword_signal():
    """Measured: MiniLM gives 'foreign currency markup' only 0.24 similarity, so the keyword signal must ground it,
    while out-of-corpus topics must not be grounded by generic words like 'card'."""
    from src.modules.retrieval.keyword_search import specific_hit

    for q in ["foreign currency markup", "standing instruction change", "statement request", "late payment fee"]:
        assert specific_hit(normalise(q), _fake_kw_rows(q)), q
    for q in ["travel insurance", "travel insurance added to my card", "reward points on my card"]:
        assert not specific_hit(normalise(q), _fake_kw_rows(q)), q


def test_rrf_rewards_agreement_between_legs():
    row = lambda cid, **s: {"chunk_id": cid, "doc_id": f"D{cid}", "section": "1", "text": "", "version": "1", **s}
    kw = [row(1, kw=0.9), row(2, kw=0.5)]
    vec = [row(2, sim=0.8), row(3, sim=0.7)]
    fused = rrf(kw, vec)
    assert [f["chunk_id"] for f in fused][0] == 2  # in both lists -> first
    assert fused[0]["scores"]["kw"] == 0.5 and fused[0]["scores"]["vec"] == 0.8
