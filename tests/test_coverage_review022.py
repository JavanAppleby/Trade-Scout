"""Scout 0.2.2 review fixes (owner rule of 2026-11-04 kept; SCOUT_SPEC 6.2, DESIGN 20):

- a 304 after a page that arrived and was not read (a lost page) proves no overlap, in the reader and
  the writer, and the pollers send the next request without validators after such a page;
- `seen_items` counts only seen ids below every new id on the page;
- the reader takes the overlap cap only when it is finite and at most one day;
- the hour-turn spacing never goes below the spacing the fetcher keeps for the host;
- a wall clock that steps back more than a few seconds makes a job due at once."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers_fakes import (FakeBoard, FakeFactSink, FakeFetcher, FakeHealthSink, FakeRawSink, FakeResponse,
                          FakeTickerMap, fetch_result)
from helpers_wire import fixture, wire_config
from scout_reader import coverage
from scout_reader.coverage import gaps

from scout.clock import ManualClock, iso
from scout.edgar_atom import EdgarAtomPoller, form_url
from scout.health import CoverageTracker, spacing_in_force
from scout.heartbeat import SourceStatusBoard
from scout.http import HttpFetcher
from scout.scheduler import CLOCK_STEP_BACK_S, EveryJob
from scout.wire_rss import WirePoller

from conftest import et_at

T0 = datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)
LOG = logging.getLogger("scout.test")
BROKEN = b"<rss><channel><item><title>x</title></item></channel></rss>"


def rec(at: datetime, result: str = "ok", *, status: int | None = None) -> dict:
    good = result in ("ok", "not_modified")
    return {"format": 1, "source": "prnewswire", "feed": "rss", "kind": "wire", "enabled": True, "gate6": True,
            "start": iso(at), "end": iso(at + timedelta(seconds=1)), "result": result,
            "http_status": status if status is not None else {"ok": 200, "not_modified": 304}.get(result),
            "items": 20 if result == "ok" else 0, "new_items": 0, "overlap": True if good else None,
            "spacing_s": 60.0, "gap_before": False, "error": None}


def _whys(rows: list[dict], cap: float | None = 1800) -> list[str]:
    end = datetime.fromisoformat(rows[-1]["end"][:-1]).replace(tzinfo=timezone.utc)
    return [g["why"] for g in gaps(rows, "prnewswire", T0 - timedelta(seconds=300), end, max_gap_s=180,
                                   overlap_max_gap_s=cap)]


def _chain(middle: list[dict], last_at: float, last: str = "not_modified") -> list[dict]:
    rows = [rec(T0 - timedelta(seconds=600 - 60 * i)) for i in range(11)]
    return rows + middle + [rec(T0 + timedelta(seconds=last_at), last),
                            rec(T0 + timedelta(seconds=last_at + 60))]


# --------------------------------------------------------------------------- a lost page and a 304


@pytest.mark.parametrize("lost", [rec(T0 + timedelta(seconds=60), "schema"),
                                  rec(T0 + timedelta(seconds=60), "too_large", status=200)])
@pytest.mark.parametrize("cap", [None, 1800])
def test_a_304_after_a_lost_page_has_no_overlap(lost: dict, cap: float | None) -> None:
    assert "no_overlap" in _whys(_chain([lost], 120), cap)  # even well within every allowance


def test_a_304_after_a_failure_without_a_page_still_covers() -> None:
    assert _whys(_chain([rec(T0 + timedelta(seconds=60), "timeout")], 900)) == []
    assert _whys(_chain([rec(T0 + timedelta(seconds=60), "timeout")], 900), None) != []  # 0.2.1: too long


def test_a_good_200_after_a_lost_page_ends_the_doubt() -> None:
    rows = _chain([rec(T0 + timedelta(seconds=60), "schema")], 120, last="ok")
    rows.append(rec(T0 + timedelta(seconds=240), "not_modified"))
    assert _whys(rows) == []


def test_page_lost() -> None:
    assert coverage.page_lost("schema", None) and coverage.page_lost("too_large", 200)
    assert not coverage.page_lost("timeout", None) and not coverage.page_lost("http_error", 503)
    assert not coverage.page_lost("ok", 200) and not coverage.page_lost("not_modified", 304)
    assert not coverage.page_lost("network", True)


def _wire(scout_dir: Path, steps: list[str]):
    """A real PR Newswire WirePoller over a real HttpFetcher; each transport step is "good" (ETag
    e1), "broken" (a new page, ETag e2, that does not parse) or "304"."""
    cfg = wire_config(scout_dir)
    clock = ManualClock(et_at("2026-11-03", "10:00"))
    sent: list[dict] = []

    def transport(url, headers, timeout_s, max_bytes):
        clock.advance(0.5)
        sent.append(dict(headers))
        step = steps.pop(0)
        if step == "good":
            return FakeResponse(200, {"ETag": '"e1"'}, fixture("prnewswire.rss"))
        if step == "broken":
            return FakeResponse(200, {"ETag": '"e2"'}, BROKEN)
        return FakeResponse(304, {}, b"")

    recs: list = []

    class Health:
        def record(self, r):
            recs.append(r)

    class Sink:
        def save(self, *a, **k):
            return "x"

        def add(self, f):
            return True

    poller = WirePoller(cfg, "prnewswire", HttpFetcher(cfg, clock, transport=transport), Sink(), Sink(), Health(),
                        SourceStatusBoard(cfg), clock, tracker=CoverageTracker(cfg))
    return poller, clock, sent, recs


def test_after_a_lost_page_the_wire_asks_without_validators(scout_dir: Path) -> None:
    """The reviewer's case: a good 200, a 200 that does not parse, then a poll 600 s later."""
    poller, clock, sent, recs = _wire(scout_dir, ["good", "broken", "good", "304"])
    for wait in (0, 60, 600, 60):
        clock.advance(wait)
        poller.poll(clock.now())
    assert [r.result for r in recs] == ["ok", "schema", "ok", "not_modified"]
    assert "If-None-Match" not in sent[2]  # after the lost page: no ETag, so no 304 can rest on it
    assert sent[1].get("If-None-Match") == '"e1"' and sent[3].get("If-None-Match") == '"e1"'


def test_the_writer_refuses_a_304_after_a_lost_page(scout_dir: Path) -> None:
    """Should a 304 still follow a lost page (records before 0.2.2, a slip), it has no overlap."""
    poller, clock, _sent, recs = _wire(scout_dir, ["good", "broken", "304"])
    poller.refetch_full = False  # as if the poller had not asked without validators
    poller.poll(clock.now())
    clock.advance(60)
    poller.poll(clock.now())
    poller.refetch_full = False
    clock.advance(60)
    poller.poll(clock.now())
    assert recs[2].result == "not_modified" and recs[2].overlap is False and recs[2].gap_before is True
    rows = [r.to_record() for r in recs]
    found = gaps(rows, "prnewswire", datetime.fromisoformat(rows[0]["start"][:-1]).replace(tzinfo=timezone.utc),
                 datetime.fromisoformat(rows[-1]["end"][:-1]).replace(tzinfo=timezone.utc), max_gap_s=180,
                 overlap_max_gap_s=1800)
    assert any(g["why"] == "no_overlap" for g in found)


def test_a_store_failure_also_makes_the_next_request_unconditional(scout_dir: Path) -> None:
    poller, clock, sent, _recs = _wire(scout_dir, ["good", "good"])

    def boom(*_a, **_k):
        raise OSError("disk")

    poller._store = boom  # type: ignore[method-assign]
    with pytest.raises(OSError):
        poller.poll(clock.now())
    del poller._store
    clock.advance(60)
    poller.poll(clock.now())
    assert "If-None-Match" not in sent[1]


def test_edgar_asks_a_form_without_validators_after_a_lost_page(scout_dir: Path) -> None:
    cfg = wire_config(scout_dir, edgar_atom_forms=["8-K"])
    clock = ManualClock(et_at("2026-11-03", "10:00"))
    asked: list[bool] = []

    class Fetcher(FakeFetcher):
        def fetch(self, source, url, *, conditional=True, max_bytes=None):
            asked.append(conditional)
            return super().fetch(source, url, conditional=conditional, max_bytes=max_bytes)

    fetcher = Fetcher()
    poller = EdgarAtomPoller(cfg, fetcher, FakeRawSink(), FakeFactSink(), FakeHealthSink(), FakeBoard(),
                             FakeTickerMap({}), clock)
    url = poller.src.url
    fetcher.results[form_url(url, "8-K")] = fetch_result("edgar_atom", "u", b"<not atom")
    poller.poll(clock.now())
    fetcher.results[form_url(url, "8-K")] = fetch_result("edgar_atom", "u", fixture("edgar_getcurrent_empty.atom"))
    poller.poll(clock.now())
    poller.poll(clock.now())
    assert asked == [True, False, True]


# --------------------------------------------------------------------------- seen ids below the new ones


def test_seen_items_counts_only_seen_ids_below_every_new_id(scout_dir: Path) -> None:
    cfg = wire_config(scout_dir)
    src = cfg.source("prnewswire")
    t = CoverageTracker(cfg)
    at = et_at("2026-11-03", "10:10")
    page = [f"a{i}" for i in range(src.max_items)]

    def poll(ids: list[str], when: datetime):
        f = fetch_result(src.name, src.url, b"x", at=when)
        new = t.new_guids(src.name, "rss", ids)
        return t.observe(src, "rss", f, now=when, guids=ids, new_items=len(new))

    poll(page, at)
    relisted = ["a5"] + [f"b{i}" for i in range(src.max_items - 1)]  # an old item re-listed on top
    r = poll(relisted, at + timedelta(seconds=60))
    assert r.overlap is True and r.seen_items == 0  # 0.2.1's overlap kept; no seen-id proof
    tail = [f"c{i}" for i in range(5)] + relisted[:src.max_items - 5]
    r = poll(tail, at + timedelta(seconds=120))
    assert r.seen_items == src.max_items - 5  # every seen id sits below the new ones


# --------------------------------------------------------------------------- the reader's cap


@pytest.mark.parametrize("value", [float("inf"), float("nan"), 86400.5, 1e300, -1, 0, "1800", True, None])
def test_the_reader_refuses_a_cap_that_is_not_a_finite_day_or_less(value) -> None:
    assert coverage.overlap_cap(value) is None


@pytest.mark.parametrize("value", [1, 1800, 86400])
def test_the_reader_takes_a_cap_up_to_one_day(value) -> None:
    assert coverage.overlap_cap(value) == float(value)


# --------------------------------------------------------------------------- the hour-turn spacing


def test_the_hour_turn_never_polls_faster_than_the_fetcher_sends(scout_dir: Path) -> None:
    turn = et_at("2026-11-03", "10:59")
    cfg = wire_config(scout_dir, host_min_spacing_s={"www.prnewswire.com": 47.0})
    assert spacing_in_force(cfg.source("prnewswire"), turn, cfg) == 47.0
    cfg = wire_config(scout_dir / "x", host_min_spacing_s={"www.prnewswire.com": 90.0})
    assert spacing_in_force(cfg.source("prnewswire"), turn, cfg) == 60.0  # never slower than min_spacing_s
    cfg = wire_config(scout_dir / "y")
    assert spacing_in_force(cfg.source("prnewswire"), turn, cfg) == 30.0


# --------------------------------------------------------------------------- the scheduler and the clock


def test_a_small_negative_elapsed_is_not_due_and_a_clock_step_back_is() -> None:
    job = EveryJob("j", lambda _now: None, 60.0)
    job.run(T0)
    assert not job.due(T0 - timedelta(seconds=CLOCK_STEP_BACK_S - 1))  # run inside another job
    assert job.due(T0 - timedelta(seconds=CLOCK_STEP_BACK_S + 1))  # the wall clock stepped back
    assert not job.due(T0 + timedelta(seconds=59)) and job.due(T0 + timedelta(seconds=60))
