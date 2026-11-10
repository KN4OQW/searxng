# SPDX-License-Identifier: AGPL-3.0-or-later
"""Result ranking for the TheChance fork.

Upstream's :py:func:`searx.results.calculate_score` sums ``weight / position``
over the engines that returned a result, with the weight multiplied by the
number of engines (so agreement counts twice), and lets the hostnames plugin
override position entirely: a *high priority* host scores as if it were #1 at
every engine, a *low priority* one scores zero.

This module keeps the same core (an engine's #1 is worth more than its #10, and
agreement between engines matters) but makes every adjustment a bounded
multiplier, so no single signal can bury a result every engine put first:

- engine agreement: ``1 + agreement_bonus * (engines - 1)``
- hostnames plugin: ``high_priority_factor`` / ``low_priority_factor``
- freshness: news results published in the last ``fresh_days`` get up to ``fresh_boost``
- site prior: hosts the search gateway has seen Claude actually use (its Valkey
  scoreboard ``gw:dom:score``), up to ``site_prior_max``
- host variety: at most ``per_host`` results from one host in the first ``window``

Settings (``ranking:`` in settings.yml), all optional::

  ranking:
    enabled: true
    agreement_bonus: 0.25
    high_priority_factor: 1.5
    low_priority_factor: 0.5
    fresh_days: 7
    fresh_boost: 0.3
    site_prior_url: "valkey://localhost:6379/1"   # unset: no site prior
    site_prior_key: "gw:dom:score"
    site_prior_max: 0.3
    per_host: 2
    window: 10
"""

from __future__ import annotations

import math
import threading
import time
import typing as t
from datetime import datetime, timezone

from searx import get_setting, logger
import searx.engines

if t.TYPE_CHECKING:
    from searx.result_types import MainResult, LegacyResult

log = logger.getChild("ranking")

_PRIOR_REFRESH = 300  # seconds
_PRIOR_TOP = 5000
_prior_lock = threading.Lock()
_prior: dict[str, float] = {}
_prior_loaded_at = 0.0


def _cfg(name: str, default: t.Any) -> t.Any:
    value = get_setting(f"ranking.{name}", default)
    return default if value is None else value


def enabled() -> bool:
    return bool(_cfg("enabled", False))


def _field(result, name: str):
    """Read a field from a MainResult (struct, attributes) or a LegacyResult /
    plain dict (mapping)."""
    if isinstance(result, dict):
        return result.get(name)
    return getattr(result, name, None)


def _host(result: "MainResult | LegacyResult") -> str:
    parsed = _field(result, "parsed_url")
    netloc = (parsed.netloc if parsed else "") or ""
    return netloc.lower().split(":")[0].removeprefix("www.")


def _engine_weight(result) -> float:
    weight = 1.0
    for name in result["engines"]:
        engine = searx.engines.engines.get(name)
        if engine is not None and hasattr(engine, "weight"):
            weight *= float(engine.weight)
    return weight


def _freshness(result) -> float:
    days = float(_cfg("fresh_days", 7))
    boost = float(_cfg("fresh_boost", 0.3))
    if boost <= 0 or days <= 0:
        return 1.0
    published = _field(result, "publishedDate")
    if not isinstance(published, datetime):
        return 1.0
    news = any("news" in (getattr(searx.engines.engines.get(n), "categories", []) or []) for n in result["engines"])
    if not news:
        return 1.0
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - published).total_seconds() / 86400
    if age_days < 0 or age_days > days:
        return 1.0
    return 1.0 + boost * (1.0 - age_days / days)


def score(result, priority) -> float:
    """Base score of one merged result (no site prior, no host variety)."""
    positions = result["positions"] or [1]
    base = sum(1.0 / p for p in positions)
    engines = max(1, len(result["engines"]))
    value = _engine_weight(result) * base * (1.0 + float(_cfg("agreement_bonus", 0.25)) * (engines - 1))
    if priority == "high":
        value *= float(_cfg("high_priority_factor", 1.5))
    elif priority == "low":
        value *= float(_cfg("low_priority_factor", 0.5))
    return value * _freshness(result)


def _site_prior() -> dict[str, float]:
    """The gateway's host scoreboard, refreshed every few minutes in-process."""
    global _prior, _prior_loaded_at  # pylint: disable=global-statement
    url = _cfg("site_prior_url", "")
    if not url:
        return {}
    if time.time() - _prior_loaded_at < _PRIOR_REFRESH:
        return _prior
    with _prior_lock:
        if time.time() - _prior_loaded_at < _PRIOR_REFRESH:
            return _prior
        try:
            import valkey  # pylint: disable=import-outside-toplevel

            db = valkey.Valkey.from_url(url, socket_timeout=0.5)
            rows = db.zrevrange(_cfg("site_prior_key", "gw:dom:score"), 0, _PRIOR_TOP - 1, withscores=True)
            _prior = {(h.decode() if isinstance(h, bytes) else h): float(s) for h, s in rows}
        except Exception as e:  # pylint: disable=broad-except
            log.warning("site prior unavailable: %s", e)
        _prior_loaded_at = time.time()
    return _prior


def apply_site_prior(results: t.Iterable) -> None:
    """Nudge hosts Claude actually uses: log-scaled, capped at site_prior_max."""
    prior = _site_prior()
    if not prior:
        return
    cap = float(_cfg("site_prior_max", 0.3))
    skip = set(_cfg("site_prior_skip_engines", ["yacy"]) or [])
    for result in results:
        # A boost for "a site Claude uses" should not lift a result only our own
        # small index returned: a live engine has to agree it is relevant first.
        if skip and set(result["engines"]) <= skip:
            continue
        points = prior.get(_host(result))
        if points:
            result.score *= 1.0 + min(cap, 0.1 * math.log10(1.0 + points))


def diversify(results: list) -> list:
    """At most `per_host` results per host in the first `window`; the rest keep
    their order right after the window."""
    per_host = int(_cfg("per_host", 2))
    window = int(_cfg("window", 10))
    if per_host <= 0 or window <= 0:
        return results
    top: list = []
    deferred: list = []
    counts: dict[str, int] = {}
    for result in results:
        if len(top) >= window:
            deferred.append(result)
            continue
        host = _host(result)
        if counts.get(host, 0) >= per_host:
            deferred.append(result)
            continue
        counts[host] = counts.get(host, 0) + 1
        top.append(result)
    return top + deferred
