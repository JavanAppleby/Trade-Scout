"""Coverage rules of 0.2.1 (owner, 2026-11-04; SCOUT_SPEC 6.2, DESIGN D7): a restart is judged like
any other poll (`scout.coverage_seed`), the idle-to-active switch uses the larger spacing, and up
to 3 failed polls between overlapping good polls are forgiven. The writer's `gap_before` and the
reader's recomputed gaps agree on the same sequences."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

from helpers_fakes import FakeBoard, FakeFetcher, FakeRawSink, fetch_result
from helpers_wire import fixture, wire_config
from scout_reader import coverage
from scout_reader.records import read_jsonl

from scout.clock import ManualClock, et_date, iso
from scout.coverage_seed import load_seeds
from scout.facts import FactWriter
from scout.health import CoverageTracker, HealthWriter
from scout.paths import ScoutPaths
from scout.store import append_jsonl
from scout.wire_rss import WirePoller

SWITCH = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)  # 04:00 ET: EDGAR goes from 600 s to 60 s
DAY = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)  # 10:00 ET
PAGE = 100  # edgar_atom max_items


class _Feed:
    """Pages for one EDGAR feed: overlapping pages repeat the newest seen ids; others are all new."""

    def __init__(self) -> None:
        self.n = 0
        self.newest: list[str] = []

    def page(self, overlap: bool, new: int = 1) -> list[str]:
        fresh = [f"g{self.n + i}" for i in range(PAGE if not overlap else new)]
        self.n += len(fresh)
        ids = (fresh + self.newest)[:PAGE]
        self.newest = ids
        return ids


def _poll(t: CoverageTracker, src, at: datetime, ids: list[str] | None, feed: str = "8-K", result: str = "ok"):
    f = fetch_result(src.name, src.url, b"x", result=result, status=200 if result == "ok" else None, at=at)
    if result != "ok":
        return t.observe(src, feed, f, now=at)
    new = t.new_guids(src.name, feed, ids or [])
    return t.observe(src, feed, f, now=at, guids=ids or [], new_items=len(new)), new


def _good(t, src, at, ids, feed="8-K"):
    return _poll(t, src, at, ids, feed)[0]


# --------------------------------------------------------------------------- writer: spacing switch, failures


def test_writer_idle_to_active_switch_uses_the_larger_spacing(scout_dir):
    cfg = wire_config(scout_dir)
    src = cfg.source("edgar_atom")
    t, pages = CoverageTracker(cfg), _Feed()
    _good(t, src, SWITCH - timedelta(seconds=1170), pages.page(False))
    before = _good(t, src, SWITCH - timedelta(seconds=570), pages.page(True))
    assert before.spacing_s == 600 and before.gap_before is False
    after = _good(t, src, SWITCH, pages.page(True))  # 570 s after a poll scheduled at 600 s
    assert after.spacing_s == 60 and after.gap_before is False
    assert _good(t, src, SWITCH + timedelta(seconds=181), pages.page(True)).gap_before is True  # 60 s both


def test_writer_forgives_up_to_three_failed_polls(scout_dir):
    cfg = wire_config(scout_dir)
    src = cfg.source("edgar_atom")
    t, pages = CoverageTracker(cfg), _Feed()
    a = DAY + timedelta(seconds=13)  # the live 8-K shape (2026-11-01 13:14:13), at active spacing
    _good(t, src, a - timedelta(seconds=60), pages.page(False))
    _good(t, src, a, pages.page(True))
    bad = _poll(t, src, a + timedelta(seconds=125), None, result="network")
    assert bad.gap_before is True and t.gap_open("edgar_atom") is True
    ok = _good(t, src, a + timedelta(seconds=185), pages.page(True))  # 185 s: within 180 + 60
    assert ok.gap_before is False and t.gap_open("edgar_atom", a + timedelta(seconds=186)) is False
    for i in range(3):
        _poll(t, src, ok_at(a, 186 + 60 * i), None, result="timeout")
    assert _good(t, src, ok_at(a, 185 + 360), pages.page(True)).gap_before is False  # 360 s: 180 + 3 x 60
    for i in range(4):
        _poll(t, src, ok_at(a, 546 + 30 * i), None, result="timeout")
    assert _good(t, src, ok_at(a, 545 + 150), pages.page(True)).gap_before is True  # four failures in a row
    _poll(t, src, ok_at(a, 700), None, result="timeout")
    assert _good(t, src, ok_at(a, 695 + 241), pages.page(True)).gap_before is True  # 241 s > 180 + 60
    _poll(t, src, ok_at(a, 940), None, result="timeout")
    assert _good(t, src, ok_at(a, 936 + 240), pages.page(False)).gap_before is True  # no overlap


def ok_at(a: datetime, s: float) -> datetime:
    return a + timedelta(seconds=s)


# --------------------------------------------------------------------------- the seed


def _write_poll(paths: ScoutPaths, rec, new: list[str]) -> None:
    """What a poller stores: the facts of the new items (first_seen_at = the poll's end), then the
    health record."""
    if new:
        target = paths.day_file(paths.facts, et_date(datetime.fromisoformat(rec.end.replace("Z", "+00:00"))))
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:  # one open per poll keeps the test fast
            fh.writelines(json.dumps({"format": 1, "source": rec.source, "feed": rec.feed, "guid": g,
                                      "first_seen_at": rec.end, "kind": "filing"}) + "\n" for g in new)
    HealthWriter(paths).record(rec)


def _seeded(cfg, paths, now) -> CoverageTracker:
    return CoverageTracker(cfg, load_seeds(paths, now, sources=("edgar_atom", "prnewswire", "stocktitan"),
                                           cap=cfg.seen_guid_cap, days=cfg.fact_dedupe_days))


def test_first_poll_after_a_short_restart_overlaps(scout_dir):
    """Live shape, 2026-11-04 01:56 ET: Scout stopped and started again within a minute or two."""
    cfg = wire_config(scout_dir)
    paths = ScoutPaths.from_config(cfg)
    src = cfg.source("edgar_atom")
    t, pages = CoverageTracker(cfg), _Feed()
    at = DAY
    for i in range(3):
        rec, new = _poll(t, src, at + timedelta(seconds=60 * i), pages.page(i > 0))
        _write_poll(paths, rec, new)
    restart = at + timedelta(seconds=120 + 100)  # 100 s after the last good poll
    unseeded, _ = _poll(CoverageTracker(cfg), src, restart, pages.newest[:])
    assert unseeded.overlap is False and unseeded.gap_before is True  # 0.2.0: every restart was a gap
    fresh = _seeded(cfg, paths, restart)
    assert fresh.gap_open("edgar_atom") is True  # nothing polled in this run yet
    rec, new = _poll(fresh, src, restart, pages.page(True, new=2))
    assert rec.overlap is True and rec.gap_before is False and new == pages.newest[:2]
    assert fresh.gap_open("edgar_atom", restart + timedelta(seconds=1)) is False


def test_a_long_outage_is_still_an_interval_gap(scout_dir):
    cfg = wire_config(scout_dir)
    paths = ScoutPaths.from_config(cfg)
    src = cfg.source("edgar_atom")
    t, pages = CoverageTracker(cfg), _Feed()
    for i in range(2):
        rec, new = _poll(t, src, DAY + timedelta(seconds=60 * i), pages.page(i > 0))
        _write_poll(paths, rec, new)
    back = DAY + timedelta(seconds=60 + 181)
    rec, _new = _poll(_seeded(cfg, paths, back), src, back, pages.page(True))
    assert rec.overlap is True and rec.gap_before is True  # anchor = the last good poll before the stop


def test_failed_polls_before_the_stop_count_after_the_restart(scout_dir):
    cfg = wire_config(scout_dir)
    paths = ScoutPaths.from_config(cfg)
    src = cfg.source("edgar_atom")
    t, pages = CoverageTracker(cfg), _Feed()
    rec, new = _poll(t, src, DAY, pages.page(False))
    _write_poll(paths, rec, new)
    HealthWriter(paths).record(_poll(t, src, DAY + timedelta(seconds=60), None, result="timeout"))
    seeds = load_seeds(paths, DAY + timedelta(seconds=200), sources=("edgar_atom",), cap=5000, days=7)
    assert seeds[("edgar_atom", "8-K")].failed == 1 and len(seeds[("edgar_atom", "8-K")].guids) == PAGE
    back = DAY + timedelta(seconds=230)  # 230 s: within 180 + 60 for one failed poll
    rec, _new = _poll(_seeded(cfg, paths, back), src, back, pages.page(True))
    assert rec.gap_before is False


def test_missing_or_corrupt_seed_fails_closed(scout_dir):
    cfg = wire_config(scout_dir)
    paths = ScoutPaths.from_config(cfg)
    src = cfg.source("edgar_atom")
    assert load_seeds(paths, DAY, sources=("edgar_atom",), cap=5000, days=7) == {}
    paths.health.mkdir(parents=True, exist_ok=True)
    paths.facts.mkdir(parents=True, exist_ok=True)
    day_file = paths.day_file(paths.health, et_date(DAY))
    day_file.write_bytes(b'{"format":1,"source":"edgar_atom","feed":"8-K","start":"2026-11-02T14:59:00Z",'
                         b'"end":"2026-11-02T14:59:00Z","result":"ok","spacing_s":"60"}\nnot json\n{"format":9}\n')
    paths.day_file(paths.facts, et_date(DAY)).write_bytes(b"\x00\xff garbage\n")
    assert load_seeds(paths, DAY, sources=("edgar_atom",), cap=5000, days=7) == {}  # unreadable spacing
    rec, _new = _poll(_seeded(cfg, paths, DAY), src, DAY, [f"x{i}" for i in range(PAGE)])
    assert rec.overlap is False and rec.gap_before is True
    assert load_seeds(object(), DAY, sources=("edgar_atom",), cap=5000, days=7) == {}  # never raises


def test_the_seed_uses_only_facts_of_good_polls_and_nothing_after_now(scout_dir):
    cfg = wire_config(scout_dir)
    paths = ScoutPaths.from_config(cfg)
    src = cfg.source("edgar_atom")
    t = CoverageTracker(cfg)
    rec, new = _poll(t, src, DAY, [f"a{i}" for i in range(PAGE)])
    _write_poll(paths, rec, new)
    # a poll that stored a fact and then failed (no health record): its page was not fully handled
    append_jsonl(paths, paths.day_file(paths.facts, et_date(DAY)),
                 {"format": 1, "source": "edgar_atom", "feed": "8-K", "guid": "half", "first_seen_at": iso(DAY + timedelta(seconds=30))})
    # written later than the restart time: no look-ahead
    later = DAY + timedelta(seconds=500)
    rec2, new2 = _poll(t, src, later, ["late"] + [f"a{i}" for i in range(PAGE - 1)])
    _write_poll(paths, rec2, new2)
    seed = load_seeds(paths, DAY + timedelta(seconds=60), sources=("edgar_atom",), cap=5000, days=7)[("edgar_atom", "8-K")]
    assert seed.anchor == DAY and "half" not in seed.guids and "late" not in seed.guids and len(seed.guids) == PAGE
    assert load_seeds(paths, DAY + timedelta(seconds=60), sources=("prnewswire",), cap=5000, days=7) == {}
    capped = load_seeds(paths, later, sources=("edgar_atom",), cap=10, days=7)[("edgar_atom", "8-K")]
    assert capped.guids[-1] == "late" and len(capped.guids) == 10 and capped.anchor == later


def test_a_restart_through_the_wire_poller_keeps_coverage(scout_dir):
    """End to end: the real WirePoller, FactWriter and HealthWriter; Scout restarts between polls."""
    cfg = wire_config(scout_dir)
    paths = ScoutPaths.from_config(cfg)
    body = fixture("prnewswire.rss")
    clock = ManualClock(DAY)

    def poller(tracker):
        fetcher = FakeFetcher()
        return fetcher, WirePoller(cfg, "prnewswire", fetcher, FakeRawSink(), FactWriter(paths, clock),
                                   HealthWriter(paths), FakeBoard(), clock, tracker=tracker)

    fetcher, wire = poller(CoverageTracker(cfg))
    for i in range(3):
        at = DAY + timedelta(seconds=60 * i)
        clock.set(at)
        fetcher.results[cfg.source("prnewswire").url] = fetch_result("prnewswire", "u", body, at=at)
        wire.poll(at)
    back = DAY + timedelta(seconds=120 + 90)
    clock.set(back)
    fetcher, wire = poller(_seeded(cfg, paths, back))
    fetcher.results[cfg.source("prnewswire").url] = fetch_result("prnewswire", "u", body, at=back)
    assert wire.poll(back) == 0  # nothing stored twice
    rows = read_jsonl(paths.day_file(paths.health, et_date(DAY)))
    assert [r["overlap"] for r in rows] == [False, True, True, True]  # 0.2.0 gave False after the restart
    assert rows[-1]["gap_before"] is False
    assert coverage.gaps(rows, "prnewswire", DAY, back, max_gap_s=180) == []


# --------------------------------------------------------------------------- writer and reader agree


def _reader_verdicts(records: list[dict], max_gap_s: float) -> list[bool]:
    """For each good poll after the first, whether the reader finds a gap since the good poll before."""
    polls = [coverage.poll_of(datetime.fromisoformat(r["start"].replace("Z", "+00:00")), r) for r in records]
    found = coverage.chain_gaps(polls, polls[0][0], polls[-1][0], max_gap_s)
    out, prev = [], None
    for at, good, _overlap, _sp in polls:
        if not good:
            continue
        if prev is not None:
            a, b = iso(prev), iso(at)
            out.append(any(g["from"] >= a and a < g["to"] <= b for g in found))
        prev = at
    return out


def _random_run(cfg, paths, rng: random.Random, restarts: bool) -> list[dict]:
    src = cfg.source("edgar_atom")
    t, pages = CoverageTracker(cfg), _Feed()
    at = SWITCH - timedelta(minutes=40) + timedelta(seconds=rng.randint(0, 600))
    out: list[dict] = []
    first = True
    steps = [(rng.random(), rng.random(), rng.choice((20, 45, 60, 61, 90, 125, 185, 300, 590, 610, 1199, 1300)),
              rng.random()) for _ in range(rng.randint(5, 40))]
    for fail_roll, overlap_roll, step, restart_roll in steps:
        if restarts and restart_roll < 0.15 and not first:
            t = _seeded(cfg, paths, at)
        if fail_roll < 0.25 and not first:
            rec = _poll(t, src, at, None, result=rng.choice(("network", "timeout", "http_error")))
            HealthWriter(paths).record(rec)
        else:
            rec, new = _poll(t, src, at, pages.page(not first and overlap_roll > 0.1, new=rng.randint(0, 3)))
            _write_poll(paths, rec, new)
            first = False
        out.append(rec.to_record())
        at += timedelta(seconds=step)
    return out


def test_writer_gap_before_agrees_with_the_reader(scout_dir, tmp_path):
    rng = random.Random(20261104)
    tally = {"gap": 0, "clear": 0, "forgiven": 0}
    for n in range(50):
        seed = rng.randint(0, 10**9)
        plain_dir, restart_dir = tmp_path / f"p{n}", tmp_path / f"r{n}"
        plain_dir.mkdir()
        restart_dir.mkdir()
        plain = _random_run(wire_config(plain_dir), ScoutPaths.from_config(wire_config(plain_dir)),
                            random.Random(seed), restarts=False)
        cfg = wire_config(restart_dir)
        restarted = _random_run(cfg, ScoutPaths.from_config(cfg), random.Random(seed), restarts=True)
        assert restarted == plain  # a restart changes no record
        writer = [r["gap_before"] for r in plain if r["result"] == "ok"][1:]
        assert writer == _reader_verdicts(plain, float(cfg.source_max_gap_s)), seed
        tally["gap"] += sum(writer)
        tally["clear"] += len(writer) - sum(writer)
        tally["forgiven"] += sum(1 for a, b in zip(plain, plain[1:])
                                 if a["result"] != "ok" and b["result"] == "ok" and not b["gap_before"])
    assert min(tally.values()) > 10, tally  # the runs cover gaps, clear polls and forgiven failures
