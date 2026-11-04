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


def _fail(records: list[dict], i: int, result: str = "timeout") -> None:
    records[i] = rec(datetime.fromisoformat(records[i]["start"].replace("Z", "+00:00")), result=result)


def test_a_failed_poll_followed_by_an_overlapping_poll_in_time_is_not_a_gap():
    """Owner, 2026-11-04: one timed-out poll is not a gap when the next good poll overlaps the one
    before it and comes within the interval limit (SS 6.2)."""
    records = chain(T0 - timedelta(minutes=5), 70)
    _fail(records, 20)
    assert gaps(records, "prnewswire", T0, T0 + timedelta(minutes=60), max_gap_s=180) == []
    _fail(records, 40, "http_error")
    _fail(records, 41, "network")  # 19:00 to 21:02 between good polls: 180 s, still within the limit
    assert gaps(records, "prnewswire", T0, T0 + timedelta(minutes=60), max_gap_s=180) == []


def test_a_failed_poll_is_a_gap_when_the_next_good_poll_does_not_overlap():
    records = chain(T0 - timedelta(minutes=5), 70)
    _fail(records, 20)
    records[21]["overlap"] = False
    got = gaps(records, "prnewswire", T0, T0 + timedelta(minutes=60), max_gap_s=180)
    assert [g["why"] for g in got] == ["failed_poll", "no_overlap"]
    assert got[0]["from"] == "2026-11-02T21:14:00Z" and got[0]["to"] == "2026-11-02T21:15:00Z"


def test_more_than_three_failed_polls_in_a_row_stay_a_gap():
    """Owner, 2026-11-04: at most 3 failed polls in a row are forgiven (SS 6.2)."""
    records = chain(T0 - timedelta(minutes=5), 70)
    for i in (20, 21, 22):  # good polls at 21:14 and 21:18: 240 s, within 180 + 3 x 60
        _fail(records, i)
    assert gaps(records, "prnewswire", T0, T0 + timedelta(minutes=60), max_gap_s=180) == []
    _fail(records, 23)  # four in a row: good polls at 21:14 and 21:19
    got = gaps(records, "prnewswire", T0, T0 + timedelta(minutes=60), max_gap_s=180)
    assert sorted({g["why"] for g in got}) == ["failed_poll", "interval"]
    assert {"from": "2026-11-02T21:14:00Z", "to": "2026-11-02T21:19:00Z", "why": "interval"} in got
    assert len([g for g in got if g["why"] == "failed_poll"]) == 4


def test_a_failed_poll_is_a_gap_when_the_good_to_good_interval_is_too_long():
    """One spacing per failed poll is allowed on top of the limit, no more."""
    at = T0 + timedelta(minutes=10)
    records = chain(T0 - timedelta(minutes=5), 16) + [rec(at + timedelta(seconds=60), result="timeout")] +         chain(at + timedelta(seconds=241), 30)  # good 21:10:00 and 21:14:01: 241 s > 180 + 60
    got = gaps(records, "prnewswire", T0, T0 + timedelta(minutes=40), max_gap_s=180)
    assert sorted(g["why"] for g in got) == ["failed_poll", "interval"]
    records[-30] = rec(at + timedelta(seconds=240))  # 240 s: within 180 + 60
    assert gaps(records, "prnewswire", T0, T0 + timedelta(minutes=40), max_gap_s=180) == []


def test_live_edgar_8k_timeout_between_two_good_polls_is_forgiven():
    """Live, edgar_atom 8-K 2026-11-01: good 13:14:13, TimeoutError 13:16:18, good 13:17:18 with
    overlap: 185 s between good polls, over 180 but within 180 + 60 (owner, 2026-11-04)."""
    day = datetime(2026, 11, 1, 13, 0, tzinfo=timezone.utc)
    kw = {"source": "edgar_atom", "feed": "8-K"}
    good = [day + timedelta(minutes=m) for m in range(0, 15)] + [datetime(2026, 11, 1, 13, 14, 13, tzinfo=timezone.utc)]
    records = [rec(t, **kw) for t in good]
    records.append(rec(datetime(2026, 11, 1, 13, 16, 18, tzinfo=timezone.utc), result="network", **kw))
    after = datetime(2026, 11, 1, 13, 17, 18, tzinfo=timezone.utc)
    records += [rec(after + timedelta(minutes=m), **kw) for m in range(0, 10)]
    assert gaps(records, "edgar_atom", day + timedelta(minutes=5), after + timedelta(minutes=9), max_gap_s=180) == []
    records[-10]["overlap"] = False  # without overlap it stays a gap
    got = gaps(records, "edgar_atom", day + timedelta(minutes=5), after + timedelta(minutes=9), max_gap_s=180)
    assert sorted(g["why"] for g in got) == ["failed_poll", "no_overlap"]


def test_idle_to_active_switch_uses_the_larger_spacing():
    """Live, every trading day: EDGAR polls every 600 s before 04:00 ET and every 60 s after, e.g.
    good 08:50:30 (600 s) then good 09:00:00 (60 s): 570 s is within 2 x 600 (owner, 2026-11-04)."""
    kw = {"source": "edgar_atom", "feed": "8-K"}
    switch = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)  # 04:00 ET
    night = [rec(switch - timedelta(seconds=30 + 600 * i), spacing=600.0, **kw) for i in range(6, 0, -1)]
    day = [rec(switch + timedelta(seconds=60 * i), spacing=60.0, **kw) for i in range(30)]
    window = (switch - timedelta(minutes=50), switch + timedelta(minutes=29))
    assert gaps(night + day, "edgar_atom", *window, max_gap_s=180) == []
    late = night[:-2] + [rec(switch - timedelta(seconds=1200), spacing=600.0, **kw)]
    assert gaps(late + day, "edgar_atom", *window, max_gap_s=180) == []  # 1200 s: within 2 x 600
    longer = night[:-2] + [rec(switch - timedelta(seconds=1201), spacing=600.0, **kw)]
    got = gaps(longer + day, "edgar_atom", *window, max_gap_s=180)
    assert [g["why"] for g in got] == ["interval"]  # 1201 s: over 2 x 600
    active = [rec(switch - timedelta(seconds=181), spacing=60.0, **kw)] + day  # both at 60 s: unchanged
    assert [g["why"] for g in gaps(active, "edgar_atom", switch, switch + timedelta(minutes=29),
                                   max_gap_s=180)] == ["interval"]


def test_failed_polls_after_the_switch_add_their_own_spacing():
    """A failed poll adds its own spacing_s, not the larger spacing of the good polls around it: last
    good poll 03:50 ET (600 s), three failures at 04:00-04:02 (60 s), good overlapping poll 04:40.
    The limit is 2 x 600 + 3 x 60 = 1380 s, so 3000 s is a gap (it was 1200 + 3 x 600 = 3000)."""
    kw = {"source": "edgar_atom", "feed": "8-K"}
    switch = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)  # 04:00 ET
    night = [rec(switch - timedelta(seconds=600 * i), spacing=600.0, **kw) for i in range(6, 0, -1)]
    fails = [rec(switch + timedelta(seconds=60 * i), result="timeout", spacing=60.0, **kw) for i in range(3)]
    window = (switch - timedelta(minutes=50), switch + timedelta(minutes=45))
    late = [rec(switch + timedelta(minutes=40 + i), spacing=60.0, **kw) for i in range(6)]
    got = gaps(night + fails + late, "edgar_atom", *window, max_gap_s=180)
    assert sorted(g["why"] for g in got) == ["failed_poll"] * 3 + ["interval"]
    soon = [rec(switch - timedelta(seconds=600) + timedelta(seconds=1380 + 60 * i), spacing=60.0, **kw)
            for i in range(30)]  # 1380 s after the 03:50 poll: forgiven
    assert gaps(night + fails + soon, "edgar_atom", *window, max_gap_s=180) == []
    assert coverage.allowance_s(180, 600, 60, [60.0, 60.0, 60.0], True) == 1380
    assert coverage.allowance_s(180, 60, 60, [60.0] * 4, True) == 180  # four: nothing forgiven


def test_a_long_run_of_failures_is_a_gap_up_to_the_next_good_poll():
    """More than 3 failures in a row: a window that starts after the last failure but before the
    next good poll still sees the gap (the writer flags that good poll)."""
    records = chain(T0 - timedelta(minutes=5), 70, step_s=30)
    for i in (20, 21, 22, 23):  # good polls at 21:04:30 and 21:07:00: 150 s, in time but 4 failures
        _fail(records, i)
    mid = datetime.fromisoformat(records[23]["start"].replace("Z", "+00:00")) + timedelta(seconds=10)
    got = gaps(records, "prnewswire", mid, T0 + timedelta(minutes=30), max_gap_s=180)
    assert got == [{"from": iso(mid), "to": "2026-11-02T21:07:00Z", "why": "failed_poll"}]


def test_a_poll_not_finished_by_the_window_end_is_not_used():
    """As of t only polls that ended by t count: a poll that started before t but ended after it
    neither forgives a failure nor supplies the poll at the end."""
    records = chain(T0 - timedelta(minutes=5), 10)
    records.append(rec(T0 + timedelta(minutes=5), result="timeout"))
    last = rec(T0 + timedelta(minutes=6))
    last["end"] = iso(T0 + timedelta(minutes=6, seconds=30))
    records.append(last)
    end = T0 + timedelta(minutes=6, seconds=10)
    assert "failed_poll" in [g["why"] for g in gaps(records, "prnewswire", T0, end, max_gap_s=180)]
    assert gaps(records, "prnewswire", T0, end + timedelta(seconds=20), max_gap_s=180) == []
    no_end = dict(last, end=None)
    assert gaps(records[:-1] + [no_end], "prnewswire", T0, end + timedelta(seconds=20), max_gap_s=180) != []


def test_only_the_listed_feeds_are_checked():
    """Owner, 2026-11-04: with `feeds` (params `gate6_feeds`) only those feeds count; EDGAR's 10-K
    is polled for the gate-5 check only. An empty list checks every feed (fail closed)."""
    end = T0 + timedelta(minutes=30)
    records = chain(T0 - timedelta(minutes=5), 40, source="edgar_atom", feed="8-K") + [
        rec(T0 - timedelta(minutes=5), source="edgar_atom", feed="10-K")]  # 10-K stops: no_poll_at_end
    assert [g["why"] for g in gaps(records, "edgar_atom", T0, end, max_gap_s=180)] == ["no_poll_at_end"]
    assert gaps(records, "edgar_atom", T0, end, max_gap_s=180, feeds=["8-K"]) == []
    assert gaps(records, "edgar_atom", T0, end, max_gap_s=180, feeds=[]) != []
    assert [g["why"] for g in gaps(records, "edgar_atom", T0, end, max_gap_s=180, feeds=["8-K", "424B"])] == [
        "missing_feed"]


def test_a_failed_poll_with_no_good_poll_after_it_is_a_gap():
    records = chain(T0 - timedelta(minutes=5), 30)
    _fail(records, 29)  # the last poll (21:24) failed; the window ends at 21:25
    got = gaps(records, "prnewswire", T0, T0 + timedelta(minutes=25), max_gap_s=180)
    assert [g["why"] for g in got] == ["failed_poll"]
    assert got[0] == {"from": "2026-11-02T21:23:00Z", "to": "2026-11-02T21:24:00Z", "why": "failed_poll"}


def test_a_failed_poll_before_any_good_poll_stays_a_gap():
    records = chain(T0 + timedelta(minutes=1), 30)
    _fail(records, 0)
    got = gaps(records, "prnewswire", T0, T0 + timedelta(minutes=25), max_gap_s=180)
    assert "failed_poll" in [g["why"] for g in got]


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
    noise = [rec(T0 + timedelta(minutes=10), source="testwire", result="timeout"),
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
    assert all(m.split(".")[0] in {"__future__", "bisect", "collections", "datetime", "typing", "scout_reader"}
               for m in mods)


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


def _walk_all(chain, start, end, max_gap_s):
    """The gap rule walked over every poll up to `end` (no shortcut), for the equivalence test:
    the limit uses the larger spacing of the two good polls, plus each failed poll's own spacing
    (at most 3 failed polls) when the later poll overlaps; a longer run of failures is a gap up to
    the next good poll."""
    raw, anchor, anchor_sp, failed, fsp, limit = [], None, 0.0, [], [], max_gap_s
    polls = [p for p in chain if p[0] <= end]
    for at, good, overlap, spacing in polls:
        limit = max(max_gap_s, 2.0 * spacing)
        if not good:
            if anchor is not None:
                failed.append((anchor, at, "failed_poll"))
                fsp.append(spacing)
            else:
                raw.append((at, at, "failed_poll"))
            continue
        s = max(anchor_sp, spacing)
        allowed = max(max_gap_s, 2.0 * s) + (sum(fsp) if 0 < len(failed) <= 3 else 0.0)
        if anchor is not None and not overlap:
            raw += [(anchor, at, "no_overlap"), *failed]
        elif anchor is not None and (at - anchor).total_seconds() > allowed:
            raw += [(anchor, at, "interval"), *failed]
        elif anchor is not None and len(failed) > 3:
            raw += [*failed, (anchor, at, "failed_poll")]
        failed, fsp, anchor, anchor_sp = [], [], at, spacing
    raw += failed
    first_good = next((p[0] for p in polls if p[1]), None)
    if first_good is None or first_good > start:
        raw.append((start, first_good or end, "no_poll_at_start"))
    if anchor is None or (end - anchor).total_seconds() > limit:
        raw.append((anchor or start, end, "no_poll_at_end"))
    return [{"from": iso(max(a, start)), "to": iso(min(b, end)), "why": w} for a, b, w in raw
            if not (b < start or a > end)]


def test_the_walk_from_the_last_good_poll_before_the_window_gives_the_same_gaps():
    import random  # noqa: PLC0415

    rng = random.Random(20261104)
    for _ in range(300):
        at, chain = T0 - timedelta(hours=2), []
        for _i in range(rng.randint(0, 60)):
            at += timedelta(seconds=rng.choice((30, 60, 60, 120, 200, 400)))
            chain.append((at, rng.random() > 0.2, rng.random() > 0.1, rng.choice((60.0, 600.0))))
        start = T0 - timedelta(minutes=rng.randint(-60, 120))
        end = start + timedelta(minutes=rng.randint(0, 90))
        got = coverage.chain_gaps(chain, start, end, 180.0)
        want = _walk_all(chain, start, end, 180.0)
        key = lambda g: (g["from"], g["to"], g["why"])  # noqa: E731
        assert sorted(got, key=key) == sorted(want, key=key)
