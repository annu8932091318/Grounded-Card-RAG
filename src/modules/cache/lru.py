"""Named in-process TTL/LRU caches with hit/miss counters: query embeddings and retrieval results (cheap, local).
Answers are cached in Postgres instead (semantic_cache, see modules/cache/semantic_cache.py). Redis is the scale-out path."""
import threading

from cachetools import TTLCache

_caches = {
    "embedding": TTLCache(maxsize=2000, ttl=3600),  # hash(normalised query) -> vector, 1 h
    "retrieval": TTLCache(maxsize=1000, ttl=24 * 3600),  # (query, tenant, corpus_hash) -> result; cleared on ingest
}
_hits = {name: 0 for name in _caches}
_misses = {name: 0 for name in _caches}
_lock = threading.Lock()


def get(name: str, key):
    with _lock:
        value = _caches[name].get(key)
        if value is None:
            _misses[name] += 1
        else:
            _hits[name] += 1
        return value


def put(name: str, key, value) -> None:
    with _lock:
        _caches[name][key] = value


def clear(name: str) -> None:
    with _lock:
        _caches[name].clear()


def stats() -> dict:
    with _lock:
        return {
            name: {
                "size": len(c),
                "hits": _hits[name],
                "misses": _misses[name],
                "hit_rate": round(_hits[name] / ((_hits[name] + _misses[name]) or 1), 4),
            }
            for name, c in _caches.items()
        }
