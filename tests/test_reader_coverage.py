"""scout_reader.coverage (SCOUT_SPEC 6.2, DESIGN 3.7, D7): the gap rule over stored health records.
Pure, stdlib only, never raises."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers_fakes import fetch_result
from helpers_wire import wire_config
from scout_reader import coverage
from scout_reader.coverage import gaps, sources_for_gate6
from scout_reader.records import read_jsonl

from scout.clock import iso
from scout.health import CoverageTracker, HealthWriter

T0 = datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)  # 16:00 ET close


def rec(at: datetime, *, source="prnewswire", feed="rss", result="ok", overlap=True, spacing=60.0, fmt=1):
    return {"format": fmt, "source": source, "feed": feed, "kind": "wire", "enabled": True, "gate6": True,
            "start": iso(at), "end": iso(at + timedelta(seconds=1)), "result": result, "http_status": 200,
            "items": 20, "new_items": 0, "overlap": overlap if result == "ok" else None, "spacing_s": spacing,
            "gap_before": False, "error": None}


def chain(start: datetime, n: int, step_s: float = 60, **kw) -> list[dict]:
    return [rec(start + timedelta(seconds=i * step_s), **kw) for i in range(n)]


def test_a_full_chain_is_covered():
    records = chain(T0 - timedelta(minutes=5), 70)
    assert gaps(records, "prnewswire", T0, T0 + timedelta(minutes=60), max_gap_s=180) == []


def test_failed_poll_is_a_gap():
    records = chain(T0 - timedelta(minutes=5), 70)
    records[20] = rec(datetime.fromisoformat(records[20]["start"].replace("Z", "+00:00")), result="timeout")
    got = gaps(records, "prnewswire", T0, T0 + timedelta(minutes=60), max_gap_s=180)
    assert [g["why"] for g in got] == ["failed_poll"]
    assert got[0]["from"] == "2026-11-02T21:14:00Z" and got[0]["to"] == "2026-11-02T21:15:00Z"


def test_poll_without_overlap_is_a_gap():
    records = chain(T0 - timedelta(minutes=5), 70)
    records[30]["overlap"] = False
    got = gaps(records, "prnewswire", T0, T0 + timedelta(minutes=60), max_gap_s=180)
    assert [g["why"] for g in got] == ["no_overlap"]


def test_null_overlap_on_a_good_poll_fails_closed():
    records = chain(T0 - timedelta(minutes=5), 70)
    records[30]["overlap"] = None
    assert [g["why"] for g in gaps(records, "prnewswire", T0, T0 + timedelta(hours=1), max_gap_s=180)] == ["no_overlap"]


def test_not_modified_counts_as_overlap():
    records = chain(T0 - timedelta(minutes=5), 70)
    records[30]["result"], records[30]["overlap"] = "not_modified", None
    assert gaps(records, "prnewswire", T0, T0 + timedelta(minutes=60), max_gap_s=180) == []


def test_long_interval_is_a_gap_and_d7_allows_idle_spacing():
    records = chain(T0 - timedelta(minutes=5), 10) + chain(T0 + timedelta(minutes=10), 50)
    got = gaps(records, "prnewswire", T0, T0 + timedelta(minutes=55), max_gap_s=180)
    assert [g["why"] for g in got] == ["interval"]
    night = chain(T0 - timedelta(minutes=10), 13, step_s=600, source="edgar_atom", feed="8-K", spacing=600)
    assert gaps(night, "edgar_atom", T0, T0 + timedelta(hours=1), max_gap_s=180) == []
    assert gaps(night, "edgar_atom", T0, T0 + timedelta(hours=1), max_gap_s=180) == \
        gaps(night, "edgar_atom", T0, T0 + timedelta(hours=1), max_gap_s=1199)


def test_no_poll_at_start_and_at_end():
    late = chain(T0 + timedelta(minutes=5), 30)
    got = gaps(late, "prnewswire", T0, T0 + timedelta(minutes=30), max_gap_s=180)
    assert got == [{"from": "2026-11-02T21:00:00Z", "to": "2026-11-02T21:05:00Z", "why": "no_poll_at_start"}]
    early = chain(T0 - timedelta(minutes=5), 10)
    got = gaps(early, "prnewswire", T0, T0 + timedelta(minutes=60), max_gap_s=180)
    assert [g["why"] for g in got] == ["no_poll_at_end"] and got[0]["to"] == "2026-11-02T22:00:00Z"
    assert gaps([], "prnewswire", T0, T0 + timedelta(minutes=1), max_gap_s=180)[0]["why"] == "no_poll_at_start"


def test_every_feed_must_be_covered():
    eight_k = chain(T0 - timedelta(minutes=5), 70, source="edgar_atom", feed="8-K")
    s1 = chain(T0 - timedelta(minutes=5), 20, source="edgar_atom", feed="S-1")
    got = gaps(eight_k + s1, "edgar_atom", T0, T0 + timedelta(minutes=60), max_gap_s=180)
    assert [g["why"] for g in got] == ["no_poll_at_end"]


def test_other_sources_and_unknown_formats_are_ignored():
    records = chain(T0 - timedelta(minutes=5), 70)
    noise = [rec(T0 + timedelta(minutes=10), source="globenewswire", result="timeout"),
             {**rec(T0 + timedelta(minutes=11)), "result": "timeout", "format": 99}, {"format": 1}, "junk", None]
    assert gaps(records + noise, "prnewswire", T0, T0 + timedelta(minutes=60), max_gap_s=180) == []


def test_gaps_outside_the_window_do_not_count():
    records = chain(T0 - timedelta(hours=2), 200)
    records[5]["result"] = "timeout"  # well before the window
    assert gaps(records, "prnewswire", T0, T0 + timedelta(minutes=60), max_gap_s=180) == []


def test_records_after_the_window_end_are_not_used():
    records = chain(T0 + timedelta(minutes=61), 5)
    assert gaps(records, "prnewswire", T0, T0 + timedelta(minutes=60), max_gap_s=180)[0]["why"] == "no_poll_at_start"


@pytest.mark.parametrize("bad", [None, 5, [{"source": "prnewswire", "start": object()}]])
def test_never_raises(bad):
    got = gaps(bad, "prnewswire", T0, T0 + timedelta(minutes=5), max_gap_s=180)  # type: ignore[arg-type]
    assert got and all(g["why"] in coverage.WHY for g in got)


def test_naive_window_fails_closed():
    got = gaps(chain(T0, 3), "prnewswire", datetime(2026, 11, 2), datetime(2026, 11, 3), max_gap_s=180)
    assert got and got[0]["why"] == "error"
    assert gaps([], "x", None, None, max_gap_s=180) == [{"from": "", "to": "", "why": "error"}]  # type: ignore


def test_empty_window_is_covered():
    assert gaps([], "prnewswire", T0, T0 - timedelta(seconds=1), max_gap_s=180) == []


def test_records_written_by_scout_are_read_back(scout_dir, paths):
    """Round trip: CoverageTracker -> HealthWriter -> day files -> gaps()."""
    cfg = wire_config(scout_dir)
    src = cfg.source("prnewswire")
    tracker, writer = CoverageTracker(cfg), HealthWriter(paths)
    for i in range(70):
        at = T0 - timedelta(minutes=5) + timedelta(seconds=60 * i)
        ids = [f"g{j}" for j in range(i, i + 3)]
        writer.record(tracker.observe(src, "rss", fetch_result(src.name, src.url, b"x", at=at), now=at, guids=ids))
    records = read_jsonl(paths.health / "2026-11-02.jsonl")
    assert gaps(records, "prnewswire", T0, T0 + timedelta(minutes=60), max_gap_s=cfg.source_max_gap_s) == []


def test_sources_for_gate6():
    beat = {"sources": {"prnewswire": "ok", "edgar_atom": "ok", "bls": "disabled", "nasdaq_halts": "ok"}}
    assert sources_for_gate6(beat, {"gate6_sources": ["prnewswire", "edgar_atom"]}) == ["edgar_atom", "prnewswire"]
    assert sources_for_gate6(beat) == ["edgar_atom", "nasdaq_halts", "prnewswire"]
    assert sources_for_gate6(beat, {"gate6_sources": "bad"}) == ["edgar_atom", "nasdaq_halts", "prnewswire"]
    assert sources_for_gate6(None) == [] and sources_for_gate6({"sources": 3}) == []


def test_coverage_module_is_stdlib_only():
    tree = ast.parse(Path(coverage.__file__).read_text(encoding="utf-8"))
    mods = {(n.module or "") if isinstance(n, ast.ImportFrom) else n.names[0].name
            for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))}
    assert all(m.split(".")[0] in {"__future__", "collections", "datetime", "typing", "scout_reader"} for m in mods)


def test_an_expected_feed_with_no_records_is_a_gap():
    """Regression: a feed that stopped writing health records more than a day before the window
    vanished from the list, and the remaining feeds counted as full coverage."""
    end = T0 + timedelta(minutes=60)
    records = chain(T0 - timedelta(minutes=5), 70, source="edgar_atom", feed="8-K")
    assert gaps(records, "edgar_atom", T0, end, max_gap_s=180) == []
    found = gaps(records, "edgar_atom", T0, end, max_gap_s=180, feeds=["8-K", "424B"])
    assert [g["why"] for g in found] == ["missing_feed"]
    both = records + chain(T0 - timedelta(minutes=5), 70, source="edgar_atom", feed="424B")
    assert gaps(both, "edgar_atom", T0, end, max_gap_s=180, feeds=["8-K", "424B"]) == []
    assert [g["why"] for g in gaps([], "edgar_atom", T0, end, max_gap_s=180, feeds=["8-K"])] == [
        "no_poll_at_start", "missing_feed"]


def test_params_list_the_feeds_of_each_gate6_source(tmp_path: Path):
    from scout.config import config_from_dict
    cfg = config_from_dict({"edgar_atom_forms": ["8-K", "424B"]}, tmp_path)
    feeds = cfg.reader_params()["gate6_feeds"]
    assert feeds["edgar_atom"] == ["8-K", "424B"] and feeds["prnewswire"] == ["rss"]
