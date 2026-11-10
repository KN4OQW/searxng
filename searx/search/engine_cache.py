# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-engine result cache (TheChance fork).

Upstream SearXNG does not cache search results.  This module caches the parsed
results of each *individual engine* in Valkey, so a repeated query (or the same
engine query reached from a different search) does not hit the upstream engine
again.

Lifecycle of a cached engine answer, with ``ttl`` resolved per engine
(:py:func:`ttl_for`):

``age < ttl``
  *fresh*: served as is, the engine is not asked.

``ttl <= age < ttl * revalidate_factor``
  *stale*: served at once, and a background thread asks the engine again and
  replaces the entry (stale-while-revalidate).  Only one refresh runs per key.

``age >= ttl * revalidate_factor``
  *expired*: the engine is asked as usual.

Independently of age, if an engine is suspended or its request fails, any
cached answer younger than ``fallback_max_age`` is served instead of nothing.

Empty answers are never stored: an empty page is often a soft block.

Settings (``engine_cache:`` in settings.yml)::

  engine_cache:
    enabled: true
    ttl: 3600                  # default seconds an answer stays fresh
    revalidate_factor: 4       # serve stale + refresh until ttl * factor
    fallback_max_age: 604800   # max age served when the engine fails
    categories:                # ttl per engine category
      news: 900
    engines:                   # ttl per engine name (wins over category)
      wikipedia: 86400

Counters per engine (``hit``, ``stale``, ``miss``, ``fallback``, ``store``) are
kept in Valkey under ``sxng:ec:stats:<engine>`` (a hash) to measure the effect.
"""

from __future__ import annotations

__all__ = ["CacheEntry", "lookup", "store", "make_key", "ttl_for", "count", "enabled"]

import hashlib
import json
import pickle
import time
import typing as t

from searx import get_setting, logger
from searx import valkeydb

log = logger.getChild("engine_cache")

KEY_PREFIX = "sxng:ec:v1:"
STATS_PREFIX = "sxng:ec:stats:"
LOCK_SUFFIX = ":lock"
LOCK_SECONDS = 30


class CacheEntry(t.NamedTuple):
    stored_at: float
    results: list[t.Any]

    @property
    def age(self) -> float:
        return time.time() - self.stored_at


def _cfg(name: str, default: t.Any) -> t.Any:
    value = get_setting(f"engine_cache.{name}", default)
    return default if value is None else value


def enabled() -> bool:
    return bool(_cfg("enabled", False)) and valkeydb.client() is not None


def stream_enabled() -> bool:
    """Streaming results need the cache: it is where late engines leave their
    answers for the page's follow-up polls."""
    return enabled() and bool(_cfg("stream", False))


def first_paint_seconds() -> float:
    return float(_cfg("first_paint", 0.6))


def ttl_for(engine_name: str, category: str) -> int:
    per_engine: dict[str, int] = _cfg("engines", {}) or {}
    if engine_name in per_engine:
        return int(per_engine[engine_name])
    per_category: dict[str, int] = _cfg("categories", {}) or {}
    if category in per_category:
        return int(per_category[category])
    return int(_cfg("ttl", 3600))


def revalidate_until(ttl: int) -> float:
    return ttl * float(_cfg("revalidate_factor", 4))


def fallback_max_age() -> int:
    return int(_cfg("fallback_max_age", 7 * 24 * 3600))


def make_key(engine_name: str, query: str, params: t.Mapping[str, t.Any]) -> str:
    """Key over the search conditions only; never over request details the
    engine adds later (URL, headers, cookies)."""
    material = {
        "e": engine_name,
        "q": query,
        "c": params.get("category"),
        "p": params.get("pageno"),
        "s": params.get("safesearch"),
        "t": params.get("time_range"),
        "l": params.get("searxng_locale"),
        "d": params.get("engine_data") or {},
    }
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()
    return f"{KEY_PREFIX}{engine_name}:{digest}"


def lookup(key: str) -> CacheEntry | None:
    db = valkeydb.client()
    if db is None:
        return None
    try:
        raw = db.get(key)
        if not raw:
            return None
        stored_at, payload = pickle.loads(raw)  # Valkey is local-only and written by us
        return CacheEntry(stored_at, pickle.loads(payload))
    except Exception as e:  # pylint: disable=broad-except
        log.warning("lookup %s failed: %s", key, e)
        return None


def snapshot(results: list[t.Any]) -> bytes | None:
    """Serialize results *before* the result container mutates them."""
    try:
        return pickle.dumps(results, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as e:  # pylint: disable=broad-except
        log.warning("results not cacheable: %s", e)
        return None


def store(key: str, payload: bytes) -> None:
    db = valkeydb.client()
    if db is None:
        return
    try:
        db.set(key, pickle.dumps((time.time(), payload)), ex=fallback_max_age())
    except Exception as e:  # pylint: disable=broad-except
        log.warning("store %s failed: %s", key, e)


def try_lock(key: str) -> bool:
    db = valkeydb.client()
    if db is None:
        return False
    try:
        return bool(db.set(key + LOCK_SUFFIX, b"1", nx=True, ex=LOCK_SECONDS))
    except Exception:  # pylint: disable=broad-except
        return False


INFLIGHT_SUFFIX = ":inflight"


def mark_inflight(key: str, seconds: float) -> bool:
    """Claim the upstream request for this key. False if another search is
    already asking the engine the same thing."""
    db = valkeydb.client()
    if db is None:
        return True
    try:
        return bool(db.set(key + INFLIGHT_SUFFIX, b"1", nx=True, ex=max(1, int(seconds) + 5)))
    except Exception:  # pylint: disable=broad-except
        return True


def clear_inflight(key: str) -> None:
    db = valkeydb.client()
    if db is None:
        return
    try:
        db.delete(key + INFLIGHT_SUFFIX)
    except Exception:  # pylint: disable=broad-except
        pass


def is_inflight(key: str) -> bool:
    db = valkeydb.client()
    if db is None:
        return False
    try:
        return bool(db.exists(key + INFLIGHT_SUFFIX))
    except Exception:  # pylint: disable=broad-except
        return False


def wait_for(key: str, timeout: float, poll: float = 0.1) -> CacheEntry | None:
    """Wait for the search that owns this key's in-flight request to finish,
    then return what it stored (None if it failed or took too long)."""
    deadline = time.time() + timeout
    while time.time() < deadline and is_inflight(key):
        time.sleep(poll)
    entry = lookup(key)
    if entry and entry.age < timeout + 5:
        return entry
    return None


def count(engine_name: str, kind: str) -> None:
    db = valkeydb.client()
    if db is None:
        return
    try:
        db.hincrby(STATS_PREFIX + engine_name, kind, 1)
    except Exception:  # pylint: disable=broad-except
        pass
