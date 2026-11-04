"""Coverage rules of 0.2.1 and 0.2.2 (owner, 2026-11-04; SCOUT_SPEC 6.2, DESIGN D7, 19): a restart is
judged like any other poll (`scout.coverage_seed`), the idle-to-active switch uses the larger
spacing, up to 3 failed polls between overlapping good polls are forgiven, and (0.2.2) a poll that
repeats a seen id covers the interval before it up to `source_overlap_max_gap_s`. The tests of the
0.2.1 rule alone turn the overlap rule off (`source_overlap_max_gap_s = 0`). The writer's
`gap_before` and the reader's recomputed gaps agree on the same sequences."""

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

    def short(self, new: int = 3) -> list[str]:
        """A short page of new ids only: overlap by page size, no seen id (0.2.2 keeps the 0.2.1 rule)."""
        fresh = [f"g{self.n + i}" for i in range(new)]
        self.n += len(fresh)
        self.newest = (fresh + self.newest)[:PAGE]
        return fresh


def _poll(t: CoverageTracker, src, at: datetime, ids: list[str] | None, feed: str = "8-K", result: str = "ok"):
    """`result` "schema": a 200 page that was not read (a lost page); "not_modified": a 304."""
    fetched = "ok" if result == "schema" else result
    status = {"ok": 200, "not_modified": 304}.get(fetched)
    f = fetch_result(src.name, src.url, b"x", result=fetched, status=status, at=at)
    if result != "ok":
        return t.observe(src, feed, f, now=at, result=result)
    new = t.new_guids(src.name, feed, ids or [])
    return t.observe(src, feed, f, now=at, guids=ids or [], new_items=len(new)), new


def _good(t, src, at, ids, feed="8-K"):
    return _poll(t, src, at, ids, feed)[0]


# --------------------------------------------------------------------------- writer: spacing switch, failures


def test_writer_idle_to_active_switch_uses_the_larger_spacing(scout_dir):
    cfg = wire_config(scout_dir, source_overlap_max_gap_s=0)  # the 0.2.1 rule alone
    src = cfg.source("edgar_atom")
    t, pages = CoverageTracker(cfg), _Feed()
    _good(t, src, SWITCH - timedelta(seconds=1170), pages.page(False))
    before = _good(t, src, SWITCH - timedelta(seconds=570), pages.page(True))
    assert before.spacing_s == 600 and before.gap_before is False
    after = _good(t, src, SWITCH, pages.page(True))  # 570 s after a poll scheduled at 600 s
    assert after.spacing_s == 60 and after.gap_before is False
    assert _good(t, src, SWITCH + timedelta(seconds=181), pages.page(True)).gap_before is True  # 60 s both


def test_writer_forgives_up_to_three_failed_polls(scout_dir):
    cfg = wire_config(scout_dir, source_overlap_max_gap_s=0)  # the 0.2.1 rule alone
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


def test_writer_failed_polls_add_their_own_spacing_after_the_switch(scout_dir):
    """03:50 ET good (600 s), three failures at 04:00-04:02 (60 s), good overlapping poll at 04:40:
    2 x 600 + 3 x 60 = 1380 s < 3000 s, a gap; the same poll at 04:13 (1380 s) is not."""
    cfg = wire_config(scout_dir)
    src = cfg.source("edgar_atom")
    for back_at, want in ((SWITCH + timedelta(minutes=40), True), (SWITCH + timedelta(seconds=780), False)):
        t, pages = CoverageTracker(cfg), _Feed()
        _good(t, src, SWITCH - timedelta(seconds=1200), pages.page(False))
        _good(t, src, SWITCH - timedelta(seconds=600), pages.page(True))
        for i in range(3):
            assert _poll(t, src, SWITCH + timedelta(seconds=60 * i), None, result="timeout").spacing_s == 60
        assert _good(t, src, back_at, pages.page(True)).gap_before is want


def test_writer_failures_before_the_first_good_poll_are_a_gap(scout_dir):
    cfg = wire_config(scout_dir)
    src = cfg.source("edgar_atom")
    t = CoverageTracker(cfg)
    assert _good(t, src, DAY, [f"s{i}" for i in range(5)]).gap_before is False  # a short page, nothing failed
    t = CoverageTracker(cfg)
    _poll(t, src, DAY, None, result="timeout")
    assert _good(t, src, DAY + timedelta(seconds=60), [f"s{i}" for i in range(5)]).gap_before is True


def test_gap_open_ignores_periodic_forms(scout_dir):
    """10-Q and 10-K are polled for the gate-5 check only (not in params `gate6_feeds`)."""
    cfg = wire_config(scout_dir, edgar_atom_forms=["8-K", "10-K"])
    src = cfg.source("edgar_atom")
    t, pages = CoverageTracker(cfg), _Feed()
    _good(t, src, DAY - timedelta(seconds=60), pages.page(False))
    _good(t, src, DAY, pages.page(True))
    _poll(t, src, DAY, None, feed="10-K", result="timeout")
    assert t.gap_open("edgar_atom", DAY + timedelta(seconds=1)) is False
    t2 = CoverageTracker(cfg)
    _poll(t2, src, DAY, None, feed="10-K", result="timeout")
    assert t2.gap_open("edgar_atom", DAY) is True  # no gate-6 feed polled yet


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
    back = DAY + timedelta(seconds=60 + 1801)  # past source_overlap_max_gap_s (1800 s)
    rec, _new = _poll(_seeded(cfg, paths, back), src, back, pages.page(True))
    assert rec.overlap is True and rec.seen_items and rec.gap_before is True  # from the last good poll before the stop


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
    seed = load_seeds(paths, DAY, sources=("edgar_atom",), cap=5000, days=7)[("edgar_atom", "8-K")]
    assert seed.guids == () and seed.spacing_s == 0.0  # an unreadable spacing reads as 0, as in the reader
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


def test_a_restart_through_the_edgar_poller_keeps_coverage(scout_dir):
    """End to end: the real EdgarAtomPoller (one fact per listing, feed = form, first_seen_at = the
    poll's end), FactWriter and HealthWriter; Scout restarts between polls. The page is full
    (max_items = its 7 entries), so only the seed can give the first poll after the restart overlap."""
    from helpers_fakes import FakeTickerMap  # noqa: PLC0415
    from helpers_wire import atom_url  # noqa: PLC0415

    from scout.edgar_atom import EdgarAtomPoller  # noqa: PLC0415

    cfg = wire_config(scout_dir, edgar_atom_forms=["8-K"], sources={"edgar_atom": {"max_items": 7}})
    paths = ScoutPaths.from_config(cfg)
    body = fixture("edgar_getcurrent_8k.atom")
    clock = ManualClock(DAY)
    names = {1234567: ("ACME", "Nasdaq"), 7654321: ("BETA", "NYSE")}

    def poller(tracker):
        fetcher = FakeFetcher()
        return fetcher, EdgarAtomPoller(cfg, fetcher, FakeRawSink(), FactWriter(paths, clock), HealthWriter(paths),
                                        FakeBoard(), FakeTickerMap(names), clock, tracker=tracker)

    fetcher, edgar = poller(CoverageTracker(cfg))
    for i in range(2):
        at = DAY + timedelta(seconds=60 * i)
        clock.set(at)
        fetcher.results[atom_url(cfg, "8-K")] = fetch_result("edgar_atom", "u", body, at=at)
        edgar.poll(at)
    back = DAY + timedelta(seconds=60 + 90)
    clock.set(back)
    fetcher, edgar = poller(_seeded(cfg, paths, back))
    fetcher.results[atom_url(cfg, "8-K")] = fetch_result("edgar_atom", "u", body, at=back)
    assert edgar.poll(back) == 0  # nothing stored twice
    rows = [r for r in read_jsonl(paths.day_file(paths.health, et_date(DAY))) if r["source"] == "edgar_atom"]
    assert [r["overlap"] for r in rows] == [False, True, True]
    assert rows[-1]["gap_before"] is False
    assert coverage.gaps(rows, "edgar_atom", DAY, back, max_gap_s=180) == []
    unseeded = CoverageTracker(cfg)
    fetcher, edgar = poller(unseeded)
    fetcher.results[atom_url(cfg, "8-K")] = fetch_result("edgar_atom", "u", body, at=back)
    edgar.poll(back)
    assert unseeded._feeds[("edgar_atom", "8-K")].last.overlap is False  # 0.2.0: the restart was a gap


# --------------------------------------------------------------------------- writer and reader agree


def _reader_verdicts(records: list[dict], max_gap_s: float, cap: float | None = None) -> list[bool]:
    """For each good poll after the first, whether the reader finds a gap since the good poll before."""
    polls = [coverage.poll_of(datetime.fromisoformat(r["start"].replace("Z", "+00:00")), r) for r in records]
    found = coverage.chain_gaps(polls, polls[0][0], polls[-1][0], max_gap_s, cap)
    out, prev = [], None
    for at, good, overlap, _sp, _seen, _nm, _lost in polls:
        if not good:
            continue
        b = iso(at)
        if prev is not None:
            a = iso(prev)
            out.append(any(g["from"] >= a and a < g["to"] <= b for g in found))
        else:  # the first good poll: failed polls before it, or no overlap (the reader's no_poll_at_start)
            out.append(not overlap or any(g["why"] == "failed_poll" and g["to"] <= b for g in found))
        prev = at
    return out


def _random_run(cfg, paths, rng: random.Random, restarts: bool) -> list[dict]:
    src = cfg.source("edgar_atom")
    t, pages = CoverageTracker(cfg), _Feed()
    at = SWITCH - timedelta(minutes=40) + timedelta(seconds=rng.randint(0, 600))
    out: list[dict] = []
    first = True  # no good poll yet: failures may come first
    steps = [(rng.random(), rng.random(), rng.choice((20, 45, 60, 61, 90, 125, 185, 300, 590, 610, 1199, 1300,
                                                      1799, 1801, 2500, 3100)),
              rng.random()) for _ in range(rng.randint(5, 40))]
    for fail_roll, overlap_roll, step, restart_roll in steps:
        if restarts and restart_roll < 0.15 and out:
            t = _seeded(cfg, paths, at)
        if fail_roll < 0.3:
            rec = _poll(t, src, at, None, result=rng.choice(("network", "timeout", "http_error", "schema")))
            HealthWriter(paths).record(rec)
        elif not first and fail_roll < 0.4:
            rec = _poll(t, src, at, None, result="not_modified")  # after a lost page: no overlap
            HealthWriter(paths).record(rec)
        else:
            if not first and 0.1 < overlap_roll < 0.3:
                ids = pages.short(new=rng.randint(1, 3))  # overlap by a short page only
            else:
                ids = pages.page(not first and overlap_roll > 0.1, new=rng.randint(0, 3))
            rec, new = _poll(t, src, at, ids)
            _write_poll(paths, rec, new)
            first = False
        out.append(rec.to_record())
        at += timedelta(seconds=step)
    return out


def _span(a: dict, b: dict) -> float:
    return (datetime.fromisoformat(b["start"][:-1]) - datetime.fromisoformat(a["start"][:-1])).total_seconds()


def test_writer_gap_before_agrees_with_the_reader(scout_dir, tmp_path):
    """Random runs (failures, seen-id pages, short pages, pages without overlap, intervals around
    every limit, restarts) with the overlap rule off, at 600 s and at the default 1800 s."""
    rng = random.Random(20261104)
    tally = {"gap": 0, "clear": 0, "forgiven": 0, "seen_forgiven": 0, "past_cap_gap": 0, "lost_304_gap": 0}
    for n in range(90):
        seed = rng.randint(0, 10**9)
        cap = (0, 600, 1800)[n % 3]
        plain_dir, restart_dir = tmp_path / f"p{n}", tmp_path / f"r{n}"
        plain_dir.mkdir()
        restart_dir.mkdir()
        plain_cfg = wire_config(plain_dir, source_overlap_max_gap_s=cap)
        plain = _random_run(plain_cfg, ScoutPaths.from_config(plain_cfg), random.Random(seed), restarts=False)
        cfg = wire_config(restart_dir, source_overlap_max_gap_s=cap)
        restarted = _random_run(cfg, ScoutPaths.from_config(cfg), random.Random(seed), restarts=True)
        assert restarted == plain  # a restart changes no record
        good = [r for r in plain if r["result"] in ("ok", "not_modified")]
        writer = [r["gap_before"] for r in good]
        assert writer == _reader_verdicts(plain, float(cfg.source_max_gap_s), cap), seed
        old = _reader_verdicts(plain, float(cfg.source_max_gap_s), None)  # 0.2.1, as with no param at all
        for i, (rec, w, o) in enumerate(zip(good, writer, old)):
            assert o or not w, seed  # the overlap rule never adds a gap
            seen = rec["seen_items"] or (rec["result"] == "not_modified" and rec["overlap"])
            if not cap or not seen:
                assert w == o, seed  # off, or a short page only: exactly the 0.2.1 rule
            if rec["result"] == "not_modified" and not rec["overlap"]:
                assert w and o, seed  # a 304 after a lost page is a gap under either rule
                tally["lost_304_gap"] += 1
            tally["seen_forgiven"] += o and not w
            if cap and i and seen and _span(good[i - 1], rec) > max(cap, 3000):
                tally["past_cap_gap"] += w  # beyond the cap and every 0.2.1 allowance: still a gap
        tally["gap"] += sum(writer)
        tally["clear"] += len(writer) - sum(writer)
        tally["forgiven"] += sum(1 for a, b in zip(plain, plain[1:])
                                 if a["result"] not in ("ok", "not_modified") and b["result"] == "ok"
                                 and not b["gap_before"])
    assert min(tally.values()) > 3, tally  # the runs cover every branch
