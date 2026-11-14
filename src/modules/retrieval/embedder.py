"""Local embeddings (all-MiniLM-L6-v2, 384-d, ONNX via fastembed). No data leaves the machine.
Declared limitation: English-only model — Hinglish queries are an expected retrieval miss (EV-031)."""
import math

from src.const.config import config
from src.helper.hash import sha256
from src.modules.cache import lru

_model = None


def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding  # WHY lazy: model load takes seconds; tests that don't embed skip it

        _model = TextEmbedding(model_name=config.embedding_model)
    return _model


def _normalise(vec) -> list[float]:
    values = [float(v) for v in vec]
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def embed_documents(texts: list[str]) -> list[list[float]]:
    return [_normalise(v) for v in _get_model().embed(texts)]


def embed_query(text: str) -> list[float]:
    key = sha256(text)
    cached = lru.get("embedding", key)
    if cached is not None:
        return cached
    vec = _normalise(next(iter(_get_model().query_embed([text]))))
    lru.put("embedding", key, vec)
    return vec
