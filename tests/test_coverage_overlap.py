"""Scout 0.2.2 (owner, 2026-11-04; SCOUT_SPEC 4.1, 6.2; DESIGN D7, 19): the overlap rule (a poll that
repeats a seen id covers the interval before it, failed polls included, up to
`source_overlap_max_gap_s`) in the reader, and the wire schedule (the EDGAR cycle lets due wire
polls run between its form requests; PR Newswire every 30 s from :58 to :05 ET)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers_answers import write_beats, write_health, write_params
from helpers_fakes import FakeResponse, write_records
from helpers_wire import fixture, wire_config
from scout_reader import coverage
from scout_reader.api import Reader
from scout_reader.coverage import gaps
from scout_reader.records import read_jsonl

from scout.app import AppContext
from scout.clock import ManualClock, iso
from scout.config import DEFAULT_SOURCES, ScoutConfig, config_from_dict
from scout.config_load import ConfigError
from scout.health import in_hour_turn, spacing_in_force
from scout.heartbeat import SourceStatusBoard
from scout.http import HARD_MIN_SPACING_S, HttpFetcher
from scout.jobs_wire import build_jobs
from scout.paths import ScoutPaths
from scout.scheduler import EveryJob, run_due

from conftest import et_at

T0 = datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)  # 16:00 ET
LOG = logging.getLogger("scout.test")
ALL_FORMS = ["8-K", "6-K", "S-1", "S-3", "F-1", "F-3", "424B", "EFFECT", "NT 10-Q", "NT 10-K", "10-Q", "10-K"]


def rec(at: datetime, *, result: str = "ok", items: int = 20, new: int = 0, seen: int | None | str = "absent",
        spacing: float = 60.0) -> dict:
    out = {"format": 1, "source": "prnewswire", "feed": "rss", "kind": "wire", "enabled": True, "gate6": True,
           "start": iso(at), "end": iso(at + timedelta(seconds=1)), "result": result, "http_status": 200,
           "items": items if result == "ok" else 0, "new_items": new if result == "ok" else 0,
           "overlap": (True if result in ("ok", "not_modified") else None), "spacing_s": spacing,
           "gap_before": False, "error": None}
    if seen != "absent":
        out["seen_items"] = seen
    return out


def _outage(seconds: float, failures: int, **last) -> list[dict]:
    """Good polls every 60 s, then `failures` failed polls, then a good poll `seconds` after the last good one."""
    rows = [rec(T0 - timedelta(seconds=600 - 60 * i)) for i in range(11)]  # up to T0
    for i in range(failures):
        rows.append(rec(T0 + timedelta(seconds=60 * (i + 1)), result="timeout"))
    rows.append(rec(T0 + timedelta(seconds=seconds), **last))
    rows += [rec(T0 + timedelta(seconds=seconds + 60 * (i + 1))) for i in range(3)]
    return rows


def _whys(rows: list[dict], cap: float | None = 1800) -> list[str]:
    end = datetime.fromisoformat(rows[-1]["end"][:-1]).replace(tzinfo=timezone.utc)
    return [g["why"] for g in gaps(rows, "prnewswire", T0 - timedelta(seconds=300), end, max_gap_s=180,
                                   overlap_max_gap_s=cap)]


# --------------------------------------------------------------------------- the reader's rule


def test_a_seen_id_covers_a_long_interval_and_any_number_of_failed_polls():
    assert _whys(_outage(900, 5, seen=3)) == []  # 15 minutes, five timeouts: nothing was missed
    assert _whys(_outage(1800, 0, seen=1)) == []  # exactly the cap
    assert _whys(_outage(900, 5, seen=3), cap=None) == ["failed_poll"] * 5 + ["interval"]  # 0.2.1


def test_beyond_the_cap_the_interval_stays_a_gap():
    assert _whys(_outage(1801, 0, seen=20)) == ["interval"]
    assert _whys(_outage(1801, 4, seen=20)) == ["failed_poll"] * 4 + ["interval"]


def test_a_short_page_alone_keeps_the_0_2_1_rule():
    """Overlap by page size (5 of 20 items, all new): no seen id, so the interval limit and the
    failed-poll allowance apply as before."""
    assert _whys(_outage(900, 5, items=5, new=5, seen=0)) == ["failed_poll"] * 5 + ["interval"]
    assert _whys(_outage(240, 1, items=5, new=5, seen=0)) == []  # 180 + 60: forgiven in time (0.2.1)
    assert _whys(_outage(240, 4, items=5, new=5, seen=0)) == ["failed_poll"] * 4 + ["interval"]  # 4 failures: no allowance


def test_records_before_0_2_2_count_a_seen_id_by_new_items_below_items():
    assert _whys(_outage(900, 5)) == []  # no seen_items key: items 20, new_items 0
    assert _whys(_outage(900, 5, new=19)) == []  # one item was not new
    assert _whys(_outage(900, 5, new=20)) != []  # every item new: overlap by... nothing seen
    assert _whys(_outage(900, 5, items=0, new=0)) != []  # an empty page shows no seen id


def test_seen_items_decides_when_present_and_unreadable_claims_nothing():
    assert _whys(_outage(900, 5, new=0, seen=0)) != []  # 0.2.2 says no id was seen: items/new not used
    for bad in (None, "3", True, -1):
        assert _whys(_outage(900, 2, seen=bad)) != [], bad
    assert coverage.seen_overlap(rec(T0, result="not_modified")) is True  # a 304
    assert coverage.seen_overlap({**rec(T0, seen=4), "overlap": False}) is False
    assert coverage.seen_overlap(rec(T0, result="timeout")) is False


def test_a_304_covers_like_a_seen_id():
    rows = _outage(900, 3)
    i = next(i for i, r in enumerate(rows) if r["start"] == iso(T0 + timedelta(seconds=900)))
    rows[i] = rec(T0 + timedelta(seconds=900), result="not_modified")
    assert _whys(rows) == []


def test_a_missing_or_bad_cap_falls_back_to_the_0_2_1_rule():
    want = ["failed_poll"] * 5 + ["interval"]
    for cap in (None, 0, -5, True, "1800"):
        assert [g["why"] for g in gaps(_outage(900, 5, seen=2), "prnewswire", T0 - timedelta(seconds=300),
                                       T0 + timedelta(seconds=1100), max_gap_s=180,
                                       overlap_max_gap_s=cap)] == want, cap  # type: ignore[arg-type]


def test_the_other_rules_are_unchanged():
    rows = _outage(900, 2, seen=3)
    rows[-4]["overlap"] = False  # the poll after the outage has no overlap at all
    rows[-4]["seen_items"] = 0
    assert _whys(rows) == ["failed_poll", "failed_poll", "no_overlap"]
    end = T0 + timedelta(seconds=900 + 180 + 400)
    assert [g["why"] for g in gaps(_outage(900, 0, seen=3), "prnewswire", T0, end, max_gap_s=180,
                                   overlap_max_gap_s=1800)] == ["no_poll_at_end"]
    assert [g["why"] for g in gaps(_outage(900, 0, seen=3), "prnewswire", T0 - timedelta(hours=1), T0,
                                   max_gap_s=180, overlap_max_gap_s=1800)] == ["no_poll_at_start"]


def test_interval_gap_is_the_one_rule():
    ig = coverage.interval_gap
    assert ig(1000, 180, 60, 60, [60.0] * 9, True, 1800) is None
    assert ig(1000, 180, 60, 60, [60.0] * 9, False, 1800) == "interval"
    assert ig(170, 180, 30, 30, [30.0] * 4, False, 1800) == "failed_poll"  # four in a row
    assert ig(170, 180, 30, 30, [30.0] * 4, True, 1800) is None  # a seen id forgives any number
    assert ig(2000, 180, 600, 600, [600.0] * 3, True, 1800) is None  # 0.2.1 allowance 3000 s still holds
    assert ig(3001, 180, 600, 600, [600.0] * 3, True, 1800) == "interval"


# --------------------------------------------------------------------------- through entry_blocks and gate 6

AS_OF = et_at("2026-11-04", "10:00")
START = et_at("2026-10-30", "20:00")


def _hole_world(scout_dir: Path, cfg: ScoutConfig, *, short_page: bool, cap_in_params: bool) -> None:
    """Healthy polls every 150 s, except a 20-minute hole this morning with four failed polls in it;
    the first poll after it repeats seen ids, or (`short_page`) holds 5 new items only."""
    extra = {} if cap_in_params else {"source_overlap_max_gap_s": None}
    write_params(scout_dir, cfg, AS_OF - timedelta(days=60), gate6_feeds={}, **extra)
    write_beats(scout_dir, AS_OF - timedelta(seconds=90), AS_OF)
    hole = et_at("2026-11-04", "09:00")
    write_health(scout_dir, START - timedelta(minutes=10), hole, every=150.0)
    back = hole + timedelta(minutes=20)
    rows = []
    for source in ("edgar_atom", "prnewswire", "stocktitan", "testwire"):
        base = {"source": source, "feed": source, "kind": "wire", "enabled": True, "gate6": True,
                "spacing_s": 60.0, "error": None}
        for i in range(4):
            at = hole + timedelta(minutes=4 * (i + 1))
            rows.append({**base, "start": iso(at), "end": iso(at + timedelta(seconds=20)), "result": "timeout",
                         "http_status": None, "items": 0, "new_items": 0, "overlap": None, "gap_before": True,
                         "error": "TimeoutError"})
        rows.append({**base, "start": iso(back), "end": iso(back + timedelta(seconds=1)), "result": "ok",
                     "http_status": 200, "items": 5 if short_page else 20, "new_items": 5 if short_page else 2,
                     "overlap": True, "gap_before": not cap_in_params or short_page,
                     "seen_items": 0 if short_page else 18})
    write_records(scout_dir, "health", rows)
    write_health(scout_dir, back + timedelta(seconds=150), AS_OF, every=150.0)


@pytest.mark.parametrize("short_page, cap_in_params, want", [
    (False, True, "false"),     # a seen id after the hole: covered (0.2.2)
    (False, False, "unknown"),  # no cap in the params: the 0.2.1 rule (fail closed)
    (True, True, "unknown"),    # a short page alone: the 0.2.1 rule
])
def test_entry_blocks_and_gate6_follow_the_overlap_rule(scout_dir: Path, cfg: ScoutConfig, short_page: bool,
                                                        cap_in_params: bool, want: str) -> None:
    _hole_world(scout_dir, cfg, short_page=short_page, cap_in_params=cap_in_params)
    write_records(scout_dir, "macro", [{
        "source": "nowflation", "first_seen_at": iso(AS_OF - timedelta(hours=2)),
        "fetched_at": iso(AS_OF - timedelta(hours=2)), "status": "ok",
        "stale_after": iso(AS_OF + timedelta(hours=28)), "reasons": [], "events": []}])
    reader = Reader(scout_dir)
    blocks = reader.entry_blocks("AAPL", AS_OF)
    assert blocks["value"] == want, blocks["reasons"]
    if want == "unknown":
        assert any(r.endswith(":interval") or r.endswith(":failed_poll") for r in blocks["reasons"])
    gate6 = reader.fresh_catalyst("AAPL", AS_OF)
    assert gate6["value"] == want, gate6["reasons"]


def test_params_carry_the_cap(scout_dir: Path) -> None:
    assert config_from_dict({}, scout_dir).reader_params()["source_overlap_max_gap_s"] == 1800
    assert config_from_dict({"source_overlap_max_gap_s": 0}, scout_dir).reader_params()[
        "source_overlap_max_gap_s"] == 0


# --------------------------------------------------------------------------- the wire schedule


class SlowSec:
    """A transport over a ManualClock: each EDGAR form request takes 10 s, every third one times out
    after 20 s (the SEC on a slow day); the wires answer in 0.5 s. Records when each host was asked."""

    def __init__(self, cfg: ScoutConfig, clock: ManualClock) -> None:
        self.cfg, self.clock = cfg, clock
        self.calls: list[tuple[datetime, str]] = []
        self.n = 0

    def __call__(self, url: str, headers, timeout_s: float, max_bytes: int) -> FakeResponse:
        self.calls.append((self.clock.now(), url))
        if "browse-edgar" in url:
            self.n += 1
            if self.n % 3 == 0:
                self.clock.advance(timeout_s)
                raise TimeoutError("slow SEC")
            self.clock.advance(10)
            return FakeResponse(200, {}, fixture("edgar_getcurrent_empty.atom"))
        self.clock.advance(0.5)
        if url == self.cfg.source("edgar_tickers").url:
            return FakeResponse(200, {}, fixture("company_tickers_exchange_excerpt.json"))
        if url == self.cfg.source("prnewswire").url:
            return FakeResponse(200, {}, fixture("prnewswire.rss"))
        if url == self.cfg.source("stocktitan").url:
            return FakeResponse(200, {}, fixture("stocktitan.rss"))
        raise OSError(url)

    def times(self, fragment: str) -> list[datetime]:
        return [at for at, url in self.calls if fragment in url]


def _run(scout_dir: Path, start: datetime, minutes: float, *, yield_between: bool = True, **cfg_kw):
    cfg = wire_config(scout_dir, edgar_atom_forms=ALL_FORMS, **cfg_kw)
    clock = ManualClock(start)
    transport = SlowSec(cfg, clock)
    ctx = AppContext(cfg=cfg, paths=ScoutPaths.from_config(cfg), clock=clock, board=SourceStatusBoard(cfg), log=LOG)
    ctx._services["fetcher"] = HttpFetcher(cfg, clock, transport=transport)
    jobs = build_jobs(ctx)
    if not yield_between:  # 0.2.1: the EDGAR cycle held the only thread to the end
        next(j for j in jobs if j.name == "edgar_atom").fn.__self__.between = None
    while clock.now() < start + timedelta(minutes=minutes):  # the main loop (scout.app.run)
        run_due(jobs, clock.now(), LOG, clock)
        clock.sleep(cfg.loop_interval_s)
    return cfg, ctx, transport


def _intervals(times: list[datetime]) -> list[float]:
    return [(b - a).total_seconds() for a, b in zip(times, times[1:])]


def test_a_slow_edgar_no_longer_delays_the_wires(scout_dir: Path) -> None:
    start = et_at("2026-11-02", "09:10")  # active hours, away from the hour turn
    cfg, ctx, transport = _run(scout_dir / "on", start, 40)
    edgar = transport.times("browse-edgar")
    cycle = (edgar[len(ALL_FORMS) - 1] - edgar[0]).total_seconds()
    assert cycle > 140  # one EDGAR cycle takes longer than source_max_gap_s ...
    for fragment, spacing in (("prnewswire", 60.0), ("stocktitan", 60.0)):
        steps = _intervals(transport.times(fragment))
        assert min(steps) >= 30  # never below the hard minimum spacing of the host
        assert max(steps) <= spacing + 20 + 2, (fragment, max(steps))  # at most one EDGAR request late
    health = read_jsonl(ctx.paths.health / "2026-11-02.jsonl")
    window = (start + timedelta(minutes=5), start + timedelta(minutes=38))
    for source in ("prnewswire", "stocktitan"):
        assert gaps(health, source, *window, max_gap_s=cfg.source_max_gap_s,
                    overlap_max_gap_s=cfg.source_overlap_max_gap_s) == [], source
    assert all(r["gap_before"] is False for r in health if r["source"] == "prnewswire"
               and r["start"] > iso(window[0]))


def test_without_the_yield_the_wires_wait_for_the_whole_edgar_cycle(scout_dir: Path) -> None:
    """The 0.2.1 behaviour, for contrast: the wires waited for the whole cycle (> 180 s)."""
    _cfg, _ctx, transport = _run(scout_dir, et_at("2026-11-02", "09:10"), 20, yield_between=False)
    assert max(_intervals(transport.times("prnewswire"))) > 150  # live, with the other jobs too: > 180


def test_one_thread_keeps_the_sec_spacing_and_each_job_runs_once_per_slot(scout_dir: Path) -> None:
    cfg, _ctx, transport = _run(scout_dir, et_at("2026-11-02", "09:10"), 20)
    sec = [at for at, url in transport.calls if "sec.gov" in url]
    assert min(_intervals(sec)) >= HARD_MIN_SPACING_S["www.sec.gov"]
    # requests never overlap: every call starts after the one before it ended (the clock only moves forward)
    assert all(a <= b for a, b in zip([t for t, _ in transport.calls], [t for t, _ in transport.calls][1:]))
    assert min(_intervals(transport.times("prnewswire"))) >= 60 - 1e-6  # never run twice in one slot


def test_prnewswire_is_polled_every_30_s_around_the_hour(scout_dir: Path) -> None:
    start = et_at("2026-11-02", "09:50")
    cfg, ctx, transport = _run(scout_dir, start, 20, sources={"edgar_atom": {"enabled": False}})
    prn = transport.times("prnewswire")
    turn = (et_at("2026-11-02", "09:58"), et_at("2026-11-02", "10:05"))
    inside = [t for t in prn if turn[0] <= t < turn[1]]
    assert 13 <= len(inside) <= 15  # 7 minutes at 30 s
    assert min(_intervals(prn)) >= 30  # the hard minimum for www.prnewswire.com
    assert max(_intervals([t for t in prn if t < turn[0]])) <= 61
    st = transport.times("stocktitan")
    assert min(_intervals(st)) >= 59  # only PR Newswire is in hour_turn_sources
    health = [r for r in read_jsonl(ctx.paths.health / "2026-11-02.jsonl") if r["source"] == "prnewswire"]
    assert {r["spacing_s"] for r in health if turn[0] <= datetime.fromisoformat(r["start"][:-1]).replace(
        tzinfo=timezone.utc) < turn[1]} == {30.0}


def test_the_hour_turn_window_and_its_extra_requests(scout_dir: Path) -> None:
    cfg = config_from_dict({}, scout_dir)
    src = cfg.source("prnewswire")
    assert in_hour_turn(et_at("2026-11-02", "09:58"), cfg) and in_hour_turn(et_at("2026-11-02", "10:04"), cfg)
    assert not in_hour_turn(et_at("2026-11-02", "10:05"), cfg) and not in_hour_turn(et_at("2026-11-02", "09:57"), cfg)
    assert not in_hour_turn(et_at("2026-11-02", "20:00"), cfg)  # outside active hours
    assert in_hour_turn(et_at("2026-11-02", "04:00"), cfg) and not in_hour_turn(et_at("2026-11-02", "03:59"), cfg)
    assert spacing_in_force(src, et_at("2026-11-02", "10:00"), cfg) == 30
    assert spacing_in_force(src, et_at("2026-11-02", "10:30"), cfg) == 60
    assert spacing_in_force(cfg.source("stocktitan"), et_at("2026-11-02", "10:00"), cfg) == 60
    # requests in one day at the scheduled spacing (an EveryJob stepped by 1 s), 0.2.1 vs 0.2.2
    day = et_at("2026-11-02", "00:00")

    def per_day(job: EveryJob) -> int:
        n = 0
        for s in range(24 * 3600):
            now = day + timedelta(seconds=s)
            if job.due(now):
                job.last_run, n = now, n + 1
        return n

    old = per_day(EveryJob("p", lambda _n: None, src.min_spacing_s, src.idle_spacing_s, cfg))
    new = per_day(EveryJob("p", lambda _n: None, src.min_spacing_s, src.idle_spacing_s, cfg,
                           interval_fn=lambda now: spacing_in_force(src, now, cfg)))
    assert old == 1440 and new - old == 112  # 16 hour turns inside 04:00-20:00 ET: 15 x 7 + 5 + 2 minutes


@pytest.mark.parametrize("data, needle", [
    ({"hour_turn_spacing_s": 20}, "hour_turn_spacing_s"),
    ({"hour_turn_sources": ["edgar_atom"]}, "hour_turn_sources"),
    ({"hour_turn_sources": ["nope"]}, "hour_turn_sources"),
    ({"hour_turn_start_min": 5, "hour_turn_end_min": 5}, "hour_turn_start_min"),
    ({"hour_turn_end_min": 60}, "hour_turn_end_min"),
    ({"source_overlap_max_gap_s": -1}, "source_overlap_max_gap_s"),
])
def test_bad_schedule_keys_are_refused(scout_dir: Path, data: dict, needle: str) -> None:
    with pytest.raises(ConfigError, match=needle):
        config_from_dict(data, scout_dir)


def test_the_hard_minimum_spacing_bounds_the_hour_turn(scout_dir: Path) -> None:
    """A wire on a host with a larger hard minimum may not be polled faster than it."""
    wire = {**DEFAULT_SOURCES["prnewswire"], "allowed_hosts": ["www.nasdaqtrader.com"],
            "url": "https://www.nasdaqtrader.com/x.rss"}
    with pytest.raises(ConfigError, match="hard minimum"):
        config_from_dict({"sources": {"slowwire": wire}, "hour_turn_sources": ["slowwire"]}, scout_dir)
    assert HARD_MIN_SPACING_S["www.prnewswire.com"] == 30.0  # 30 s is the fastest PR Newswire allows
