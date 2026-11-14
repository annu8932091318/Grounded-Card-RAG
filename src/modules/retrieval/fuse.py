"""Reciprocal Rank Fusion: score = sum over legs of 1 / (k + rank). Scale-free, nothing to tune."""


def rrf(kw_rows: list[dict], vec_rows: list[dict], k: int = 60) -> list[dict]:
    fused: dict[int, dict] = {}
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
