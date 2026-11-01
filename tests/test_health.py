"""scout.health (SCOUT_SPEC 6.1, 6.2; DESIGN 5.2, D7): health records, overlap, gaps, status."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from helpers_fakes import fetch_result
from helpers_wire import wire_config
from scout_reader.records import read_jsonl

from scout.clock import iso
from scout.health import CoverageTracker, HealthWriter, board_state, gap_limit_s, spacing_in_force, status_for
from scout.models import HealthRecord

DAY = datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)  # 09:00 ET
NIGHT = datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)  # 01:00 ET


def guids(*ns: int) -> list[str]:
    return [f"g{n}" for n in ns]


def poll(tracker, src, at, ids, result="ok", feed="rss", **kw):
    f = fetch_result(src.name, src.url, b"x", result=result, status=200 if result == "ok" else None, at=at)
    return tracker.observe(src, feed, f, now=at, guids=ids if result in ("ok",) else (), **kw)


def test_health_writer_appends_by_et_date_of_start(paths, write_audit):
    rec = HealthRecord(source="prnewswire", feed="rss", kind="wire", enabled=True, gate6=True,
                       start="2026-11-03T03:00:00Z", end="2026-11-03T03:00:01Z", result="ok", http_status=200,
                       items=20, new_items=2, overlap=True, spacing_s=60.0, gap_before=False)
    with write_audit.watching(paths.root):
        HealthWriter(paths).record(rec)
    rows = read_jsonl(paths.health / "2026-11-02.jsonl")
    assert rows == [rec.to_record()] and rows[0]["format"] == 1


def test_overlap_by_seen_guid_or_short_page(scout_dir):
    cfg = wire_config(scout_dir)
    src = cfg.source("prnewswire")  # max_items 20
    t = CoverageTracker(cfg)
    first = poll(t, src, DAY, guids(*range(20)))
    assert first.overlap is False and first.gap_before is True  # a full page with nothing seen before
    assert first.items == 20 and first.new_items == 0
    second = poll(t, src, DAY + timedelta(seconds=60), guids(*range(5, 25)), new_items=5)
    assert second.overlap is True and second.gap_before is False and second.new_items == 5
    jump = poll(t, src, DAY + timedelta(seconds=120), guids(*range(100, 120)))
    assert jump.overlap is False and jump.gap_before is True  # 20 new items: some may have passed
    short = CoverageTracker(cfg)
    s1 = poll(short, src, DAY, guids(1, 2, 3))
    assert s1.overlap is True and s1.gap_before is False  # fewer than the feed holds: nothing passed


def test_page_size_counts_left_out_entries(scout_dir):
    cfg = wire_config(scout_dir)
    src = cfg.source("edgar_atom")  # max_items 100
    t = CoverageTracker(cfg)
    assert poll(t, src, DAY, guids(*range(90)), feed="8-K", page_size=100).overlap is False
    assert poll(CoverageTracker(cfg), src, DAY, guids(*range(90)), feed="8-K").overlap is True


def test_not_modified_overlaps_and_failures_are_gaps(scout_dir):
    cfg = wire_config(scout_dir)
    src = cfg.source("prnewswire")
    t = CoverageTracker(cfg)
    poll(t, src, DAY, guids(1, 2))
    nm = poll(t, src, DAY + timedelta(seconds=60), [], result="not_modified")
    assert nm.overlap is True and nm.gap_before is False and nm.result == "not_modified"
    bad = poll(t, src, DAY + timedelta(seconds=120), [], result="timeout")
    assert bad.overlap is None and bad.gap_before is True and bad.items == 0
    assert t.gap_open("prnewswire") is True
    good = poll(t, src, DAY + timedelta(seconds=150), guids(2, 3))
    assert good.gap_before is False and t.gap_open("prnewswire") is False


def test_schema_result_override(scout_dir):
    cfg = wire_config(scout_dir)
    src = cfg.source("prnewswire")
    rec = CoverageTracker(cfg).observe(src, "rss", fetch_result(src.name, src.url, b"<x"), now=DAY,
                                       result="schema", error="bad")
    assert rec.result == "schema" and rec.overlap is None and rec.gap_before and rec.error == "bad"


def test_interval_gap_uses_d7_and_idle_spacing(scout_dir):
    cfg = wire_config(scout_dir)
    src = cfg.source("edgar_atom")  # 60 s by day, 600 s overnight
    assert spacing_in_force(src, DAY, cfg) == 60 and spacing_in_force(src, NIGHT, cfg) == 600
    assert gap_limit_s(cfg, 60) == 180 and gap_limit_s(cfg, 600) == 1200
    t = CoverageTracker(cfg)
    poll(t, src, NIGHT, guids(1), feed="8-K")
    ok_night = poll(t, src, NIGHT + timedelta(seconds=600), guids(1, 2), feed="8-K")
    assert ok_night.gap_before is False and ok_night.spacing_s == 600
    late = poll(t, src, NIGHT + timedelta(seconds=600 + 1201), guids(2, 3), feed="8-K")
    assert late.gap_before is True
    day = CoverageTracker(cfg)
    poll(day, src, DAY, guids(1), feed="8-K")
    assert poll(day, src, DAY + timedelta(seconds=181), guids(1), feed="8-K").gap_before is True


def test_gap_open_by_age_and_per_feed(scout_dir):
    cfg = wire_config(scout_dir)
    src = cfg.source("edgar_atom")
    t = CoverageTracker(cfg)
    assert t.gap_open("edgar_atom") is True  # never polled
    poll(t, src, DAY, guids(1), feed="8-K")
    poll(t, src, DAY, guids(9), feed="424B")
    assert t.gap_open("edgar_atom", DAY + timedelta(seconds=100)) is False
    assert t.gap_open("edgar_atom", DAY + timedelta(seconds=181)) is True
    poll(t, src, DAY + timedelta(seconds=60), [], feed="424B", result="http_error")
    assert t.gap_open("edgar_atom") is True and sorted(t.feeds("edgar_atom")) == ["424B", "8-K"]


def test_seen_guids_are_capped(scout_dir):
    cfg = wire_config(scout_dir, seen_guid_cap=100)
    src = cfg.source("prnewswire")
    t = CoverageTracker(cfg)
    poll(t, src, DAY, guids(*range(150)))
    assert t.new_guids("prnewswire", "rss", guids(0, 49, 50, 149)) == ["g0", "g49"]
    assert t.new_guids("prnewswire", "rss", ["a", "a", "b"]) == ["a", "b"]


@pytest.mark.parametrize("results, status", [
    ([], "starting"), (["ok"], "ok"), (["ok", "not_modified"], "ok"), (["ok", "timeout"], "stale"),
    (["backoff"], "stale"), (["http_error"], "stale"), (["schema", "ok"], "unavailable"), (["no_http"], "unavailable"),
    (["too_large"], "unavailable"), (["host_cap"], "unavailable"), (["refused"], "refused"),
])
def test_status_for(results, status):
    assert status_for(results) == status


def test_board_state():
    s = board_state(True, "ok", iso(DAY), None, False)
    assert s.status == "ok" and s.last_ok == "2026-11-02T14:00:00Z" and not s.gap_open
    assert board_state(False, "ok", None, None, True).status == "disabled"
