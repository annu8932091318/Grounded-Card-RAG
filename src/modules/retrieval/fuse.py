"""Reciprocal Rank Fusion: score = sum over legs of 1 / (k + rank). Scale-free, nothing to tune."""


def rrf(kw_rows: list[dict], vec_rows: list[dict], k: int = 60) -> list[dict]:
    fused: dict[str, dict] = {}
    for leg, rows, score_field in (("kw", kw_rows, "kw"), ("vec", vec_rows, "sim")):
        for rank, row in enumerate(rows, start=1):
            entry = fused.setdefault(row["chunk_id"], {
                "chunk_id": row["chunk_id"], "doc_id": row["doc_id"], "section": row["section"],
                "text": row["text"], "version": row["version"],
                "scores": {"kw": None, "vec": None, "rrf": 0.0},
            })
            entry["scores"][leg] = round(float(row[score_field]), 4)
            entry["scores"]["rrf"] += 1.0 / (k + rank)
    for e in fused.values():
        e["scores"]["rrf"] = round(e["scores"]["rrf"], 5)
    return sorted(fused.values(), key=lambda e: e["scores"]["rrf"], reverse=True)


def top_k_with_leg_winners(fused: list[dict], kw_rows: list[dict], vec_rows: list[dict], top_k: int) -> list[dict]:
    """Top-k by RRF, but each leg's #1 hit is always kept.
    WHY (measured): POL-SVC-006 is the keyword leg's #1 for "statement request" but ranks low on vectors
    (one long list chunk), so plain RRF pushed it out of the top 5 behind chunks that were mediocre on both legs."""
    result = fused[:top_k]
    for rows in (kw_rows, vec_rows):
        if not rows or any(e["chunk_id"] == rows[0]["chunk_id"] for e in result):
            continue
        winner = next(e for e in fused if e["chunk_id"] == rows[0]["chunk_id"])
        result = result[:top_k - 1] + [winner] if len(result) >= top_k else result + [winner]
    return result
