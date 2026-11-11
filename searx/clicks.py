# SPDX-License-Identifier: AGPL-3.0-or-later
"""Click signals and background link checks (TheChance fork).

Result links stay direct links: no redirect page, no added latency, nothing that
could be abused as an open redirect. A small script (thechance-signals.js)
reports clicks and quick returns with ``navigator.sendBeacon``:

- ``POST /thechance/click``: a result was opened. Counts the click, adds to the
  host's score on the search gateway's scoreboard, and checks the page in a
  background thread. A page that is clearly gone (DNS failure, connection
  refused, 404, 410) is marked dead for a few days; 403, 429, 5xx and timeouts
  are "unknown" and change nothing, since many sites refuse bots.
- ``POST /thechance/bounce``: the reader came back to the results within
  ``bounce_seconds``. Counts a bounce for that page.

Every result carries a signature (HMAC of its URL with the instance secret), so
signals can only be recorded for URLs this instance actually served.

Stored per URL (Valkey hash ``gw:url:<hash>``: ``c`` clicks, ``b`` bounces,
``dead`` epoch) and per host (``gw:dom:score``). No IPs, no user data.
:py:mod:`searx.ranking` reads them back.

Settings (``clicks:``)::

  clicks:
    enabled: true
    valkey_url: "valkey://localhost:6379/1"
    click_host_points: 2
    bounce_seconds: 15
    dead_days: 3
    recheck_hours: 6
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

from searx import get_setting, logger

log = logger.getChild("clicks")

URL_PREFIX = "gw:url:"
CHECK_PREFIX = "gw:url:chk:"
HOST_KEY = "gw:dom:score"
DEAD_STATUS = {404, 410}
_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0"

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tc-linkcheck")
_db_lock = threading.Lock()
_db = None


def _cfg(name: str, default):
    value = get_setting(f"clicks.{name}", default)
    return default if value is None else value


def enabled() -> bool:
    return bool(_cfg("enabled", False)) and bool(_cfg("valkey_url", ""))


def db():
    global _db  # pylint: disable=global-statement
    if _db is None:
        with _db_lock:
            if _db is None:
                import valkey  # pylint: disable=import-outside-toplevel

                _db = valkey.Valkey.from_url(_cfg("valkey_url", ""), socket_timeout=0.5)
    return _db


def url_key(url: str) -> str:
    return URL_PREFIX + hashlib.sha256(url.encode()).hexdigest()[:24]


def sign(url: str) -> str:
    secret = str(get_setting("server.secret_key")).encode()
    return hmac.new(secret, b"tc-click:" + url.encode(), hashlib.sha256).hexdigest()[:20]


def verify(url: str, sig: str) -> bool:
    return bool(url) and bool(sig) and hmac.compare_digest(sign(url), sig)


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def record_click(url: str) -> None:
    try:
        pipe = db().pipeline()
        pipe.hincrby(url_key(url), "c", 1)
        pipe.expire(url_key(url), 90 * 86400)
        host = host_of(url)
        if host:
            pipe.zincrby(HOST_KEY, float(_cfg("click_host_points", 2)), host)
        pipe.execute()
    except Exception as e:  # pylint: disable=broad-except
        log.warning("click not recorded: %s", e)
        return
    queue_page(url)
    schedule_check(url)


PAGE_QUEUE_KEY = "gw:crawl:pages"


def queue_page(url: str) -> None:
    """Ask the gateway's seeder to index this exact page (plus one level of
    links) in YaCy on its next run."""
    try:
        db().zadd(PAGE_QUEUE_KEY, {url: time.time()}, nx=True)
    except Exception as e:  # pylint: disable=broad-except
        log.warning("page not queued for crawling: %s", e)


def score_results(engine_name: str, results) -> None:
    """Every real search feeds the crawl scoreboard: +`result_host_points` per
    result per engine, for the result's host. YaCy's own results do not count,
    so the index is not steered toward sites it already has."""
    if engine_name in set(_cfg("score_skip_engines", ["yacy"]) or []):
        return
    points = float(_cfg("result_host_points", 0.5))
    bumps: dict[str, float] = {}
    for result in results:
        url = result.get("url") if isinstance(result, dict) else getattr(result, "url", None)
        host = host_of(url) if url else ""
        if host and "." in host:
            bumps[host] = bumps.get(host, 0.0) + points
    if not bumps:
        return
    try:
        pipe = db().pipeline()
        for host, inc in bumps.items():
            pipe.zincrby(HOST_KEY, inc, host)
        pipe.execute()
    except Exception as e:  # pylint: disable=broad-except
        log.warning("results not scored: %s", e)


def record_bounce(url: str, dwell_ms: int) -> None:
    if dwell_ms < 0 or dwell_ms > float(_cfg("bounce_seconds", 15)) * 1000:
        return
    try:
        db().hincrby(url_key(url), "b", 1)
    except Exception as e:  # pylint: disable=broad-except
        log.warning("bounce not recorded: %s", e)


def schedule_check(url: str) -> None:
    """At most one check per URL per `recheck_hours`, off the request thread."""
    try:
        key = CHECK_PREFIX + url_key(url)[len(URL_PREFIX) :]
        if not db().set(key, b"1", nx=True, ex=int(float(_cfg("recheck_hours", 6)) * 3600)):
            return
    except Exception:  # pylint: disable=broad-except
        return
    _pool.submit(_check, url)


def _public(host: str, port: int) -> bool:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise
    return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


def probe(url: str, timeout: float = 6.0) -> str:
    """'alive', 'dead' or 'unknown'. Follows up to 3 redirects, re-checking that
    every hop is a public address."""
    from curl_cffi import requests  # pylint: disable=import-outside-toplevel

    current = url
    for _ in range(4):
        parts = urlsplit(current)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return "unknown"
        try:
            if not _public(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)):
                return "unknown"
        except socket.gaierror:
            return "dead"  # the name no longer resolves
        try:
            resp = requests.get(
                current,
                headers={"User-Agent": _UA, "Range": "bytes=0-2047"},
                timeout=timeout,
                allow_redirects=False,
                stream=True,
                impersonate="firefox",
            )
            status = resp.status_code
            location = resp.headers.get("location")
            resp.close()
        except Exception as e:  # pylint: disable=broad-except
            text = str(e).lower()
            if "refused" in text or "could not resolve" in text:
                return "dead"
            return "unknown"  # timeouts, TLS trouble, resets: not proof of death
        if status in (301, 302, 303, 307, 308) and location:
            current = urljoin(current, location)
            continue
        if status in DEAD_STATUS:
            return "dead"
        if 200 <= status < 300 or status == 416:
            return "alive"
        return "unknown"
    return "unknown"


CONFIRM_SECONDS = 300


def _check(url: str, confirming: bool = False) -> None:
    verdict = probe(url)
    if verdict == "dead" and not confirming:
        # One DNS hiccup or a brief outage must not bury a page for days:
        # look again in a few minutes and only then call it dead.
        timer = threading.Timer(CONFIRM_SECONDS, lambda: _pool.submit(_check, url, True))
        timer.daemon = True
        timer.start()
        return
    try:
        key = url_key(url)
        if verdict == "dead":
            db().hset(key, "dead", str(int(time.time())))
            db().expire(key, 90 * 86400)
        elif verdict == "alive":
            db().hdel(key, "dead")
    except Exception as e:  # pylint: disable=broad-except
        log.warning("link check result not stored: %s", e)
    log.debug("link check %s: %s", url, verdict)


def signals(urls: list[str]) -> dict[str, tuple[int, int, bool]]:
    """url -> (clicks, bounces, dead) for the URLs that have any signal."""
    if not urls:
        return {}
    dead_after = time.time() - float(_cfg("dead_days", 3)) * 86400
    out: dict[str, tuple[int, int, bool]] = {}
    try:
        pipe = db().pipeline()
        for url in urls:
            pipe.hmget(url_key(url), "c", "b", "dead")
        rows = pipe.execute()
    except Exception as e:  # pylint: disable=broad-except
        log.warning("signals unavailable: %s", e)
        return {}
    for url, (c, b, dead) in zip(urls, rows):
        if c is None and b is None and dead is None:
            continue
        out[url] = (int(c or 0), int(b or 0), bool(dead) and float(dead) > dead_after)
    return out
