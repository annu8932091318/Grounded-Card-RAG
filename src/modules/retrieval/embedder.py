"""Embeddings with a fallback chain (EMBEDDING_MODEL, in order):

  1. gemini-embedding-001    Gemini API (primary): best ranking on this corpus, 768-d
  2. gemini-embedding-2      Gemini API (fallback): separate free quota, 768-d
  3. local:BAAI/bge-small-en-v1.5   fastembed on CPU, 384-d: no network, no quota, never "runs out"
  4. (all failed)            retrieval continues on BM25 alone; the cache matches exact keys only

Measured on "how much is the foreign currency markup?" vs "what is foreign currency markup?":
gemini-embedding-2 0.940, gemini-embedding-001 0.918, bge-small 0.938 ("forex markup charges?": 0.904 / 0.892 / 0.861;
"...markup on my Classic card?", which must NOT match: ~0.86 / ~0.81 / 0.849). bge-small replaced all-MiniLM-L6-v2
(0.851 / 0.634 / 0.769: it ranked the Classic-card question above the forex paraphrase, so no threshold worked).

WHY one Chroma collection per model: vectors of different models live in different spaces (even different sizes), so a
fallback model is only useful if the corpus was ALSO embedded with it. Ingest embeds every chunk with every model; at
query time the first model that answers picks its own collection, and its own thresholds (config, per model).
Gemini task types: RETRIEVAL_DOCUMENT (chunks, with the doc title) and RETRIEVAL_QUERY (questions; the semantic cache
reuses the same vector, so one call serves both). Vectors are re-normalised to unit length: cosine = dot product.
Groq / xAI are not in the chain: neither offers an embeddings endpoint for these keys (Groq /embeddings -> 404).
"""
import math
import re

import httpx

from src.const.config import config
from src.helper.async_utils import CircuitBreaker
from src.helper.hash import sha256
from src.modules.cache import lru
from src.modules.llm.errors import ProviderRateLimited, ProviderRetryable, ProviderTimeout, from_status
from src.modules.telemetry.logger import log

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:{method}"
LOCAL_PREFIX = "local:"
_breakers: dict[str, CircuitBreaker] = {}
_local_models: dict = {}


class EmbeddingUnavailable(Exception):
    """Every embedding model failed."""


def models() -> list[str]:
    return list(config.embedding_models)


def is_local(model: str) -> bool:
    return model.startswith(LOCAL_PREFIX)


def local_fallback() -> str:
    """The local model in the chain (used for the quarantine collection: never searched, so never worth API quota)."""
    return next((m for m in models() if is_local(m)), f"{LOCAL_PREFIX}BAAI/bge-small-en-v1.5")


def collection_suffix(model: str) -> str:
    """'gemini-embedding-2' -> 'gemini_embedding_2' (Chroma collection names: letters, digits, _ and -)."""
    return re.sub(r"[^a-zA-Z0-9]+", "_", model).strip("_")


def breaker(model: str) -> CircuitBreaker:
    if model not in _breakers:
        _breakers[model] = CircuitBreaker(f"embed:{model}", config.circuit_fails_to_open, config.circuit_open_s)
    return _breakers[model]


def _normalise(vec) -> list[float]:
    values = [float(v) for v in vec]
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


# --- local (fastembed, ONNX on CPU) ------------------------------------------------------------------------

def _local(model: str):
    name = model[len(LOCAL_PREFIX):]
    if name not in _local_models:
        from fastembed import TextEmbedding  # WHY lazy: loading takes seconds and is only needed when we fall back

        _local_models[name] = TextEmbedding(model_name=name)
    return _local_models[name]


# --- Gemini API ------------------------------------------------------------------------------------------------

def _post(model: str, method: str, body: dict) -> dict:
    try:
        r = httpx.post(URL.format(model=model, method=method), json=body, timeout=config.embedding_timeout_s,
                       headers={"x-goog-api-key": config.gemini_api_key})  # header, never the URL: keys stay out of logs
    except httpx.TimeoutException as e:
        raise ProviderTimeout(f"embed {type(e).__name__}") from e
    except httpx.TransportError as e:
        raise ProviderRetryable(f"embed {type(e).__name__}") from e
    if r.status_code != 200:
        raise from_status("gemini-embed", r.status_code, r.text, r.headers)
    return r.json()


def _request(model: str, text: str, task: str, title: str | None = None) -> dict:
    req = {"model": f"models/{model}", "content": {"parts": [{"text": text}]}, "taskType": task,
           "outputDimensionality": config.embedding_dim}
    if title and task == "RETRIEVAL_DOCUMENT":
        req["title"] = title  # Gemini uses the title to embed documents better
    return req


# --- public ----------------------------------------------------------------------------------------------------

def embed_documents(texts: list[str], model: str, titles: list[str] | None = None) -> list[list[float]]:
    """Ingest: all chunks with ONE model. Raises on failure (ingest records it per model)."""
    if is_local(model):
        return [_normalise(v) for v in _local(model).embed(texts)]
    if not config.gemini_api_key:
        raise EmbeddingUnavailable("GEMINI_API_KEY not set")
    out: list[list[float]] = []
    size = config.embedding_batch_size
    titles = titles or [None] * len(texts)
    for i in range(0, len(texts), size):
        reqs = [_request(model, t, "RETRIEVAL_DOCUMENT", tt) for t, tt in zip(texts[i:i + size], titles[i:i + size], strict=True)]
        out += [_normalise(e["values"]) for e in _post(model, "batchEmbedContents", {"requests": reqs})["embeddings"]]
    return out


def embed_query(text: str, model: str) -> list[float]:
    """One question vector from one model (LRU-cached: retrieval and the semantic cache share it)."""
    key = (model, sha256(text))
    cached = lru.get("embedding", key)
    if cached is not None:
        return cached
    if is_local(model):
        vec = _normalise(next(iter(_local(model).query_embed([text]))))
    else:
        if not config.gemini_api_key:
            raise EmbeddingUnavailable("GEMINI_API_KEY not set")
        vec = _normalise(_post(model, "embedContent", _request(model, text, "RETRIEVAL_QUERY"))["embedding"]["values"])
    lru.put("embedding", key, vec)
    return vec


def query_vector(text: str, only: list[str] | None = None) -> tuple[str | None, list[float] | None]:
    """First model in the chain that answers -> (model, vector); (None, None) if all fail (callers degrade, never crash).
    `only` restricts the chain to models whose Chroma collection actually holds the corpus."""
    chain = [m for m in models() if only is None or m in only]
    for model in chain:
        cached = lru.get("embedding", (model, sha256(text)))
        if cached is not None:
            return model, cached
        if not breaker(model).allow():
            continue
        try:
            vec = embed_query(text, model)
            breaker(model).success()
            if model != chain[0]:
                log("embedding_fallback_used", level="warning", model=model)
            return model, vec
        except Exception as e:
            permanent = getattr(e, "permanent", False) or isinstance(e, EmbeddingUnavailable)
            log("embedding_failed", level="warning", model=model, error=type(e).__name__, detail=str(e)[:160],
                action="next_model")
            if permanent or isinstance(e, ProviderRateLimited):
                breaker(model).open_for(600 if permanent else config.circuit_open_s)  # quota / bad key: stop trying a while
            else:
                breaker(model).failure()
    log("embedding_unavailable", level="error", tried=chain)
    return None, None
