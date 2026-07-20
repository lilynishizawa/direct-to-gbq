"""
condition_synonyms.py -- disk-cached MeSH synonym expansion for the web
app's condition search.

mesh_lookup.py talks to NIH's MeSH API, which is a network round trip (or
several, per keyword) -- too slow to redo on every search. This wraps it
with a JSON file cache keyed on the lowercased search text, so a given
condition word only ever hits NIH's API once; every later search (by
anyone, across app restarts) reads the cached synonym list instead.

Not built for high-concurrency production use (the cache file is a plain
JSON read-modify-write, not safe against many processes writing at once) --
fine for this app's local/personal-scale usage. If the cache file ever gets
corrupted, just delete it; it will be rebuilt on demand.
"""

import json
import os
import threading

import mesh_lookup

CACHE_PATH = os.path.join(os.path.dirname(__file__), "mesh_synonym_cache.json")

_lock = threading.Lock()
_client = mesh_lookup.MeshClient()


def _load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_cache(cache):
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


_cache = _load_cache()


def expand(condition: str) -> list[str]:
    """Given a user-typed condition, return every term worth searching for:
    the original text plus every MeSH synonym on file for it. Falls back to
    just the original text if NIH's MeSH API can't be reached."""
    key = condition.strip().lower()
    if not key:
        return []

    with _lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached

    try:
        result = mesh_lookup.lookup_keyword(_client, condition)
        terms = mesh_lookup.flat_synonyms(result)
    except mesh_lookup.MeshApiError:
        terms = [condition]

    with _lock:
        _cache[key] = terms
        _save_cache(_cache)

    return terms