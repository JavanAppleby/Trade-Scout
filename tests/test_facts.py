"""scout.facts (SCOUT_SPEC 6.3, 10.2; DESIGN D5): fact_id, dedup, daily files by ET date."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from scout_reader.records import read_jsonl

from scout.clock import ManualClock
from scout.facts import FactWriter, make_fact_id
from scout.models import Fact


def fact(guid: str, ticker: str | None, seen: str = "2026-11-02T14:00:00Z", source: str = "prnewswire") -> Fact:
    return Fact(fact_id=make_fact_id(source, guid, ticker), kind="wire", ticker=ticker, cik=None, cls="other",
                source=source, feed="rss", guid=guid, url=None, title="t", published_at="2026-11-02T13:00:00Z",
                first_seen_at=seen, raw_sha256=None)


def test_fact_id_is_d5():
    assert make_fact_id("prnewswire", "g", "ACME") == hashlib.sha256(b"prnewswire|g|ACME").hexdigest()
    assert make_fact_id("prnewswire", "g", None) == hashlib.sha256(b"prnewswire|g|").hexdigest()
    assert make_fact_id("prnewswire", "g", "A") != make_fact_id("prnewswire", "g", "B")
    assert make_fact_id("prnewswire", "g", "A") != make_fact_id("testwire", "g", "A")


def test_add_dedups_and_writes_by_et_date(paths, clock, write_audit):
    w = FactWriter(paths, clock)
    with write_audit.watching(paths.root):
        assert w.add(fact("g1", "ACME")) is True
        assert w.add(fact("g1", "ACME")) is False
        assert w.add(fact("g1", "BETA")) is True
        assert w.add(fact("g2", None, seen="2026-11-03T03:00:00Z")) is True  # 22:00 ET on 2026-11-02
    rows = read_jsonl(paths.facts / "2026-11-02.jsonl")
    assert [r["ticker"] for r in rows] == ["ACME", "BETA", None]
    assert rows[0]["class"] == "other" and rows[0]["format"] == 1 and w.seen(rows[0]["fact_id"])


def test_dedup_survives_restart_over_today_and_yesterday(paths):
    day1 = ManualClock(datetime(2026, 11, 2, 14, tzinfo=timezone.utc))
    FactWriter(paths, day1).add(fact("g1", "ACME"))
    day2 = ManualClock(datetime(2026, 11, 3, 14, tzinfo=timezone.utc))
    assert FactWriter(paths, day2).add(fact("g1", "ACME", seen="2026-11-03T14:00:00Z")) is False
    day4 = ManualClock(datetime(2026, 11, 5, 14, tzinfo=timezone.utc))
    assert FactWriter(paths, day4, days=2).add(fact("g1", "ACME", seen="2026-11-05T14:00:00Z")) is True


def test_a_restart_never_stores_last_weeks_feed_items_again(paths):
    """Regression: only today's and yesterday's files were loaded, so after a Monday restart a quiet
    EDGAR feed still listing Friday's entries stored them again as new facts."""
    friday = ManualClock(datetime(2026, 10, 30, 14, tzinfo=timezone.utc))
    FactWriter(paths, friday).add(fact("g1", "ACME", seen="2026-10-30T14:00:00Z"))
    monday = ManualClock(datetime(2026, 11, 2, 14, tzinfo=timezone.utc))
    assert FactWriter(paths, monday).add(fact("g1", "ACME")) is False
    later = ManualClock(datetime(2026, 11, 9, 14, tzinfo=timezone.utc))  # past the 7-day window
    assert FactWriter(paths, later).add(fact("g1", "ACME", seen="2026-11-09T14:00:00Z")) is True


def test_memory_is_pruned_after_the_day_rolls(paths, clock):
    w = FactWriter(paths, clock, days=2)
    w.add(fact("old", "A"))
    clock.advance(3 * 86400)
    w.add(fact("new", "A", seen="2026-11-05T14:00:00Z"))
    assert not w.seen(make_fact_id("prnewswire", "old", "A"))


def test_bad_first_seen_is_refused(paths, clock):
    import pytest

    with pytest.raises(ValueError):
        FactWriter(paths, clock).add(fact("g", "A", seen="yesterday"))
