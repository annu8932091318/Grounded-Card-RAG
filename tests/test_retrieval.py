"""Chunking, quarantine scan, normalisation, BM25 keyword leg and RRF (the store-free parts of retrieval)."""
from src.const.config import config
from src.helper.money import format_inr, parse_amount
from src.helper.text import extract_numbers, normalise
from src.modules.guardrails.input_guard import scan_corpus_doc
from src.modules.retrieval.chunker import chunk_doc, parse_doc
from src.modules.retrieval.fuse import rrf
from src.modules.retrieval.keyword_search import bm25, build_index, terms

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
    assert extract_numbers("capped at 1.25x, or 3x your limit") == {"1.25", "3"}  # multipliers are checked too
    assert parse_amount("2 lakh") == 200000 and format_inr(150000) == "1,50,000"


def test_keyword_query_terms_drop_stopwords():
    assert terms("what is the markup if i use my card in singapore") == ["markup", "use", "card", "singapore"]
    assert terms("the a of") == []


def _corpus_rows() -> list[dict]:
    rows = []
    for p in CORPUS.glob("*.md"):
        raw = p.read_text(encoding="utf-8")
        if scan_corpus_doc(raw):
            continue  # quarantined docs are never searched
        d = parse_doc(raw)
        rows += [{"chunk_id": f"{d['doc_id']}:{c['section']}", "doc_id": d["doc_id"], "section": c["section"],
                  "text": c["text"], "text_norm": c["text_norm"], "version": d["version"]} for c in chunk_doc(d)]
    return rows


def test_bm25_ranks_the_right_document_first():
    index = build_index(_corpus_rows())
    for query, expected in [("foreign currency markup", "POL-FEE-007"), ("late payment fee", "POL-FEE-007"),
                            ("re-kyc overdue", "POL-KYC-008"), ("adverse action reason codes", "POL-GOV-009")]:
        top = bm25(index, terms(normalise(query)), 3)
        assert top and top[0]["doc_id"] == expected, (query, [r["doc_id"] for r in top])
    assert bm25(index, [], 5) == []


def test_bm25_rare_term_beats_common_term():
    rows = [{"chunk_id": "a", "doc_id": "A", "text_norm": "card card card fee"},
            {"chunk_id": "b", "doc_id": "B", "text_norm": "card singapore markup"}]
    rows += [{"chunk_id": f"x{i}", "doc_id": "X", "text_norm": "card limit"} for i in range(5)]
    top = bm25(build_index(rows), ["card", "singapore"], 2)
    assert top[0]["chunk_id"] == "b"


def _fake_kw_rows(query: str) -> list[dict]:
    """The real BM25 leg over the corpus (quarantined docs excluded), without ChromaDB."""
    return bm25(build_index(_corpus_rows()), terms(normalise(query)), 20)


def test_relevance_gate_keyword_signal():
    """Measured: MiniLM gives 'foreign currency markup' only 0.24 similarity, so the keyword signal must ground it,
    while out-of-corpus topics must not be grounded by generic words like 'card'."""
    from src.modules.retrieval.keyword_search import specific_hit

    for q in ["foreign currency markup", "standing instruction change", "statement request", "late payment fee"]:
        assert specific_hit(normalise(q), _fake_kw_rows(q)), q
    for q in ["travel insurance", "travel insurance added to my card", "reward points on my card"]:
        assert not specific_hit(normalise(q), _fake_kw_rows(q)), q


def test_leg_winner_is_never_dropped():
    from src.modules.retrieval.fuse import top_k_with_leg_winners

    row = lambda cid, **s: {"chunk_id": cid, "doc_id": f"D{cid}", "section": "1", "text": "", "version": "1", **s}
    kw = [row(9, kw=0.4)] + [row(i, kw=0.1) for i in range(1, 6)]   # chunk 9: clear keyword winner
    vec = [row(i, sim=0.5) for i in range(1, 6)] + [row(9, sim=0.05)]  # ...but last on vectors
    fused = rrf(kw, vec)
    assert 9 not in [f["chunk_id"] for f in fused[:2]]
    kept = [f["chunk_id"] for f in top_k_with_leg_winners(fused, kw, vec, 2)]
    assert 9 in kept and 1 in kept and len(kept) == 2  # vector winner (1) kept too


def test_rrf_rewards_agreement_between_legs():
    row = lambda cid, **s: {"chunk_id": cid, "doc_id": f"D{cid}", "section": "1", "text": "", "version": "1", **s}
    kw = [row(1, kw=0.9), row(2, kw=0.5)]
    vec = [row(2, sim=0.8), row(3, sim=0.7)]
    fused = rrf(kw, vec)
    assert [f["chunk_id"] for f in fused][0] == 2  # in both lists -> first
    assert fused[0]["scores"]["kw"] == 0.5 and fused[0]["scores"]["vec"] == 0.8


def test_keyword_coverage_ignores_tiny_and_generic_words():
    """Regression (measured with Gemini/MiniLM probes): the old any-term rule grounded "what's the weather tomorrow"
    through the token 's'. Coverage counts only the question's own specific terms (>= 3 letters, not generic)."""
    from src.modules.retrieval.keyword_search import coverage

    for q in ["what's the weather tomorrow", "give me stock market tips", "travel insurance added to my card"]:
        assert coverage(normalise(q), _fake_kw_rows(q)) < 0.34, q
    for q in ["foreign currency markup", "late payment fee", "statement request"]:
        assert coverage(normalise(q), _fake_kw_rows(q)) >= 0.34, q


def test_gate_rules_per_model(monkeypatch):
    from src.modules.retrieval import retriever

    monkeypatch.setattr(config, "retrieval_gate", {"local:m": (0.30, 0.16), "gemini-embedding-001": (0.55, 0.55)})
    monkeypatch.setattr(config, "retrieval_keyword_coverage", 0.34)
    assert retriever._passes_gate("local:m", 0.31, 0.0)            # strong semantic match alone
    assert retriever._passes_gate("local:m", 0.20, 0.5)            # moderate match + real keyword match
    assert not retriever._passes_gate("local:m", 0.20, 0.25)       # moderate match, weak keywords
    assert not retriever._passes_gate("gemini-embedding-001", 0.50, 1.0)  # Gemini's scale is higher
    assert retriever._passes_gate(None, 0.0, 0.5) and not retriever._passes_gate(None, 0.0, 0.34)  # all embeddings down
