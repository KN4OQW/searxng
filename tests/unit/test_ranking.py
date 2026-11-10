# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=missing-module-docstring,missing-class-docstring,missing-function-docstring
"""TheChance fork: searx.ranking."""

from urllib.parse import urlparse

from searx import ranking
from tests import SearxTestCase


def _result(url, engines, positions):
    return {"url": url, "parsed_url": urlparse(url), "engines": set(engines), "positions": positions}


class _Scored:
    def __init__(self, url, score):
        self.parsed_url = urlparse(url)
        self.score = score

    def get(self, key, default=None):
        return getattr(self, key, default)


class TestScore(SearxTestCase):

    def test_position_matters(self):
        first = ranking.score(_result("https://a.org/", ["e1"], [1]), None)
        tenth = ranking.score(_result("https://b.org/", ["e1"], [10]), None)
        self.assertGreater(first, tenth)

    def test_agreement_matters(self):
        one = ranking.score(_result("https://a.org/", ["e1"], [1]), None)
        three = ranking.score(_result("https://a.org/", ["e1", "e2", "e3"], [1, 1, 1]), None)
        self.assertGreater(three, one)

    def test_priority_is_bounded(self):
        # A high-priority host at #10 must not beat a normal #1 (upstream let it).
        high_tenth = ranking.score(_result("https://h.org/", ["e1"], [10]), "high")
        normal_first = ranking.score(_result("https://n.org/", ["e1"], [1]), None)
        self.assertLess(high_tenth, normal_first)
        # and low priority no longer scores zero
        self.assertGreater(ranking.score(_result("https://l.org/", ["e1"], [1]), "low"), 0)


class TestDiversify(SearxTestCase):

    def test_caps_hosts_in_window(self):
        results = [_Scored(f"https://same.org/{i}", 10 - i) for i in range(5)] + [_Scored("https://other.org/", 1)]
        out = ranking.diversify(results)
        hosts = [r.parsed_url.netloc for r in out]
        self.assertEqual(hosts[:3], ["same.org", "same.org", "other.org"])
        self.assertEqual(len(out), len(results))
