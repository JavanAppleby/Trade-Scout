"""The market feed for the reads (0.3.2; SCOUT_SPEC 19.5, DESIGN 24; build plan amendments A to G).
A fake Alpaca service as the fetcher's transport, ManualClock, a temporary Scout folder. No network,
no real key."""

from __future__ import annotations

import json
import logging
import socket
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import make_config
from helpers_fakes import FakeResponse
from helpers_market import (
    CLOSE, FAKE_ID, FAKE_SECRET, KEY, LAST, FakeAlpaca, asked_end, closes_answer, delayed_answer, live_answer, ok,
    poller,
)

from scout import __version__
from scout.alpaca_data import Price
from scout.clock import ManualClock, et_datetime, parse_hhmm
from scout.market_feed import (
    DELAYED, FAILS_BEFORE_SLOWING, LIVE, MarketPoller, candidate, choose, market_day,
)
from scout.paths import ScoutPaths
from scout.raw import RawStore
from scout.reads_market import market_file, market_lines

WED = date(2026, 11, 11)  # a trading day (EST); the previous one is Tuesday 2026-11-10


def et(day: date, hhmm: str) -> datetime:
    return et_datetime(day, parse_hhmm(hhmm))


@pytest.fixture
def setup(scout_dir: Path):
    cfg = make_config(scout_dir, reads_enabled=True)
    return cfg, ScoutPaths.from_config(cfg)


def lines(paths: ScoutPaths, day: date = WED) -> list[dict]:
    return market_lines(paths, day)


# --------------------------------------------------------------------------- requests


def test_poll_asks_both_legs_with_key_headers_and_plain_user_agent(setup) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    fake = FakeAlpaca(clock)
    p = poller(cfg, paths, clock, fake)
    p.tick(clock.now())
    assert fake.legs() == [LIVE, DELAYED, "closes"]
    for _leg, url, headers, timeout in fake.calls:
        assert url.startswith("https://data.alpaca.markets/v2/stocks/")
        assert headers["APCA-API-KEY-ID"] == FAKE_ID and headers["APCA-API-SECRET-KEY"] == FAKE_SECRET
        assert headers["User-Agent"] == f"Scout/{__version__}" and "@" not in headers["User-Agent"]
        assert cfg.contact_email not in json.dumps(headers)
        assert timeout == cfg.reads.market_timeout_s == 5.0
    clock.advance(60)
    p.tick(clock.now())
    assert fake.legs()[3:] == [LIVE, DELAYED]  # the day's previous closes are held: asked once a day
    clock.set(et(date(2026, 11, 12), "04:05"))
    fake.prev_day = WED
    p.tick(clock.now())
    assert fake.legs()[5:] == [LIVE, DELAYED, "closes"]  # a new day asks again


def test_record_shape_append_only_inside_scouts_folder(setup, write_audit) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    p = poller(cfg, paths, clock, FakeAlpaca(clock))
    with write_audit.watching(paths.root):
        p.tick(clock.now())
        clock.advance(60)
        p.tick(clock.now())
    recs = lines(paths)
    assert [r["kind"] for r in recs] == ["market", "market"]
    rec = recs[0]
    assert set(rec) == {"format", "kind", "first_seen_at", "at", "feed", "prev_close_feed", "prev_day", "trading_day",
                        "symbols"}
    assert rec["format"] == 1 and rec["feed"] == "iex" and rec["prev_close_feed"] == "sip"
    assert rec["prev_day"] == "2026-11-10" and rec["trading_day"] == "2026-11-11"
    assert rec["first_seen_at"].endswith("Z") and rec["at"].endswith("Z")
    spy = rec["symbols"]["SPY"]
    assert spy["last"] == "681.2" and spy["prev_close"] == "679.10"
    assert spy["change_pct"] == str((Decimal("681.2") / Decimal("679.10") - 1).quantize(Decimal("0.000001")))
    assert rec["symbols"]["IWM"]["change_pct"] == "-0.002152"
    assert rec["at"] == min(v["trade_at"] for v in rec["symbols"].values())  # the oldest trade
    assert set(rec["symbols"]) == {"SPY", "QQQ", "IWM"}
    path = market_file(paths, WED)
    assert path.parent == paths.reads / "market" and path.name == "2026-11-11.jsonl"
    events = {Path(p).resolve() for _e, p in write_audit.events}
    assert all(str(e).startswith(str(paths.root.resolve())) for e in events)


def test_newest_candidate_wins(setup) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    p = poller(cfg, paths, clock, FakeAlpaca(clock))
    assert p.poll(clock.now()).record["feed"] == "iex"  # live in session
    # before 08:00 ET the last IEX trade is yesterday's: the delayed whole-market price wins
    clock.set(et(WED, "07:00"))
    fake = FakeAlpaca(clock, **{LIVE: lambda url, now: live_answer(et(date(2026, 11, 10), "16:59"))})
    out = poller(cfg, paths, clock, fake).poll(clock.now())
    assert out.state == "recorded" and out.record["feed"] == "sip_delayed"
    # a tie goes to the delayed prices
    at = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)
    a = {"feed": LIVE, "at": at, "symbols": {}}
    b = {"feed": DELAYED, "at": at, "symbols": {}}
    assert choose([a, b])["feed"] == DELAYED and choose([b, a])["feed"] == DELAYED
    assert choose([None]) is None


def test_candidate_needs_every_symbol_price_and_no_future_time() -> None:
    now = et(WED, "10:00")
    start = et(WED, "04:00")
    closes = {s: Decimal(str(c).lstrip("#")) for s, c in CLOSE.items()}
    prices = {s: Price(s, Decimal(str(p)), now - timedelta(seconds=5)) for s, p in LAST.items()}
    syms = ("SPY", "QQQ", "IWM")
    cand, why = candidate(LIVE, prices, closes, syms, now, start)
    assert cand is not None and why == ""
    assert candidate(LIVE, {k: v for k, v in prices.items() if k != "IWM"}, closes, syms, now, start)[1] == \
        "missing_symbol"
    assert candidate(DELAYED, {}, closes, syms, now, start)[1] == "no_recent_bar"
    assert candidate(LIVE, prices, {"SPY": Decimal(1)}, syms, now, start)[1] == "no_prev_close"
    future = dict(prices, QQQ=Price("QQQ", Decimal(1), now + timedelta(seconds=61)))
    assert candidate(LIVE, future, closes, syms, now, start)[1] == "future_time"
    near = dict(prices, QQQ=Price("QQQ", Decimal(1), now + timedelta(seconds=60)))
    assert candidate(LIVE, near, closes, syms, now, start)[0] is not None
    yesterday = dict(prices, SPY=Price("SPY", Decimal(1), start - timedelta(seconds=1)))
    assert candidate(LIVE, yesterday, closes, syms, now, start)[1] == "old"


@pytest.mark.parametrize("when, prev", [
    (et(WED, "05:00"), date(2026, 11, 10)),  # before the open
    (et(WED, "11:00"), date(2026, 11, 10)),  # in session
    (et(WED, "18:00"), date(2026, 11, 10)),  # after the close
    (et(date(2026, 11, 9), "10:00"), date(2026, 11, 6)),  # Monday gives Friday
    (et(date(2026, 11, 27), "10:00"), date(2026, 11, 25)),  # the day after Thanksgiving gives Wednesday
])
def test_previous_close_before_open_in_session_after_close(setup, when: datetime, prev: date) -> None:
    cfg, paths = setup
    clock = ManualClock(when)
    asked: list[str] = []

    def closes(url, now):
        asked.append(url)
        return closes_answer(prev)

    fake = FakeAlpaca(clock, prev_day=prev, closes=closes,
                      **{DELAYED: lambda url, now: delayed_answer(asked_end(url)),
                         LIVE: lambda url, now: live_answer(now - timedelta(seconds=2))})
    out = poller(cfg, paths, clock, fake).poll(clock.now())
    assert out.prev_day == prev and out.state == "recorded"
    assert out.record["prev_day"] == prev.isoformat() and out.record["symbols"]["SPY"]["prev_close"] == "679.10"
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(asked[0]).query))
    assert q["start"] == f"{(datetime(prev.year, prev.month, prev.day) - timedelta(days=7)):%Y-%m-%d}T00:00:00Z"
    # the request ends at 00:00 ET of the trading day: hours old, never "recent" whole-tape data
    assert q["end"] == et(market_day(when), "00:00").astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert (when - et(market_day(when), "00:00")).total_seconds() >= 4 * 3600


def test_a_wrong_dated_close_gives_no_record(setup) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    fake = FakeAlpaca(clock, closes=lambda url, now: closes_answer(date(2026, 11, 9)))
    p = poller(cfg, paths, clock, fake)
    p.tick(clock.now())
    assert [(r["kind"], r["reason"]) for r in lines(paths)] == [("miss", "no_prev_close")]


def test_previous_close_is_the_whole_markets_never_the_iex_daily_bar(setup) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    out = poller(cfg, paths, clock, FakeAlpaca(clock)).poll(clock.now())
    # the snapshot's dailyBar / prevDailyBar (IEX-only prices 1 and 2) are never used
    assert {v["prev_close"] for v in out.record["symbols"].values()} == {"679.10", "608.49", "241.65"}
    assert out.record["prev_close_feed"] == "sip"


def test_stale_data_writes_no_record(setup) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "04:10"))  # no IEX trade yet today, no whole-market bar in the window yet
    fake = FakeAlpaca(clock, **{LIVE: lambda url, now: live_answer(et(date(2026, 11, 10), "16:59")),
                                DELAYED: ok({"bars": {}, "next_page_token": None})})
    p = poller(cfg, paths, clock, fake)
    p.tick(clock.now())
    assert [(r["kind"], r["reason"]) for r in lines(paths)] == [("miss", "stale")]
    # data that passes every check but is older than reads_market_max_age_s is only "stale" too
    clock.set(et(WED, "09:00"))
    old = FakeAlpaca(clock, **{LIVE: lambda url, now: live_answer(now - timedelta(minutes=40)),
                               DELAYED: lambda url, now: delayed_answer(now - timedelta(minutes=45))})
    out = poller(cfg, paths, clock, old).poll(clock.now())
    assert out.state == "stale" and out.record is None and out.chosen is not None
    assert p.interval_at(clock.now()) == cfg.reads.market_poll_s


@pytest.mark.parametrize("when", [et(WED, "03:59"), et(WED, "20:00"), et(date(2026, 11, 7), "10:00"),
                                  et(date(2026, 11, 26), "10:00")])
def test_no_request_outside_sessions_or_on_closed_days(setup, when: datetime) -> None:
    cfg, paths = setup
    clock = ManualClock(when)
    fake = FakeAlpaca(clock)
    poller(cfg, paths, clock, fake).tick(clock.now())
    assert fake.calls == [] and not market_file(paths, when.date()).exists()


def test_market_day_rolls_at_four() -> None:
    assert market_day(et(WED, "03:59")) == date(2026, 11, 10) and market_day(et(WED, "04:00")) == WED
    assert market_day(et(date(2026, 11, 7), "12:00")) == date(2026, 11, 6)


# --------------------------------------------------------------------------- faults


def _faults(clock: ManualClock):
    return {
        "403": FakeResponse(403, {}, b'{"message": "forbidden"}'), "429": FakeResponse(429, {}, b""),
        "500": FakeResponse(500, {}, b"oops"), "timeout": socket.timeout("timed out"), "oserror": OSError("reset"),
        "bad_json": FakeResponse(200, {}, b"{not json"), "shape": ok([1, 2, 3]),
        "missing": live_answer(clock.now(), skip=("IWM",)),
        "redirect": FakeResponse(301, {"location": "https://paper-api.alpaca.markets/v2/orders"}, b""),
    }


@pytest.mark.parametrize("fault", ["403", "429", "500", "timeout", "oserror", "bad_json", "shape", "missing",
                                   "redirect"])
def test_faults_write_no_record_and_never_raise(setup, fault: str) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    route = _faults(clock)[fault]
    fake = FakeAlpaca(clock, **{LIVE: route, DELAYED: route, "closes": route})
    p = poller(cfg, paths, clock, fake, raw=RawStore(paths))
    for _ in range(3):
        p.tick(clock.now())
        clock.advance(300)
    assert [r for r in lines(paths) if r["kind"] == "market"] == []
    assert all(r["kind"] == "miss" and isinstance(r["reason"], str) for r in lines(paths))


def test_redirect_is_not_followed(setup) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    moved = FakeResponse(301, {"location": "https://paper-api.alpaca.markets/v2/orders"}, b"")
    fake = FakeAlpaca(clock, **{LIVE: moved, DELAYED: moved, "closes": moved})
    out = poller(cfg, paths, clock, fake).poll(clock.now())
    assert fake.legs() == [LIVE, DELAYED, "closes"]  # one call each; Location never asked
    assert all("paper-api" not in url for _l, url, _h, _t in fake.calls)
    assert out.state == "failed" and out.record is None and out.reason == "http_301"


def test_a_timeout_on_the_live_request_skips_the_rest(setup) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    fake = FakeAlpaca(clock, **{LIVE: socket.timeout("slow")})
    out = poller(cfg, paths, clock, fake).poll(clock.now())
    assert fake.legs() == [LIVE] and out.reason == "timeout"
    assert {n: leg.code for n, leg in out.legs.items()} == {LIVE: "timeout", DELAYED: "skipped", "closes": "skipped"}


def test_state_change_logged_once(setup, caplog) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    fake = FakeAlpaca(clock, **{LIVE: FakeResponse(500, {}, b""), DELAYED: FakeResponse(500, {}, b"")})
    log = logging.getLogger("t.market")
    p = poller(cfg, paths, clock, fake, log=log)
    with caplog.at_level(logging.DEBUG, logger="t.market"):
        for _ in range(10):
            p.tick(clock.now())
            clock.advance(60)
        warned = [r for r in caplog.records if r.levelno >= logging.WARNING and r.name == "t.market"]
        assert len(warned) == 1 and "no usable answer" in warned[0].getMessage()
        fake.routes[LIVE] = lambda url, now: live_answer(now - timedelta(seconds=2))
        p.tick(clock.now())
        clock.advance(60)
        p.tick(clock.now())
    infos = [r for r in caplog.records if r.levelno == logging.INFO and r.name == "t.market"]
    assert len(infos) == 1 and "recording again" in infos[0].getMessage()
    # the per-request lines are quiet (DEBUG)
    assert not [r for r in caplog.records if r.name == "t.market" and r.getMessage().startswith("fetch ")
                and r.levelno > logging.DEBUG]


def test_429_backs_off_and_recovers(setup) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    fake = FakeAlpaca(clock, **{LIVE: FakeResponse(429, {}, b"")})
    p = poller(cfg, paths, clock, fake)
    out = p.poll(clock.now())
    assert out.state == "failed" and fake.legs() == [LIVE]  # the host backs off: the other requests wait
    clock.advance(60)
    n = len(fake.calls)
    idle = p.poll(clock.now())
    assert idle.state == "idle" and len(fake.calls) == n and p._fails == 1  # no request: not a failure
    fake.routes[LIVE] = lambda url, now: live_answer(now - timedelta(seconds=2))
    clock.advance(600)
    assert p.poll(clock.now()).state == "recorded" and p._fails == 0


def test_repeated_failures_slow_then_reset(setup) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    fake = FakeAlpaca(clock, **{LIVE: FakeResponse(500, {}, b""), DELAYED: FakeResponse(500, {}, b"")})
    p = poller(cfg, paths, clock, fake)
    seen = []
    for _ in range(8):
        p.tick(clock.now())
        seen.append(p.interval_at(clock.now()))
        clock.advance(60)
    assert seen[:FAILS_BEFORE_SLOWING - 1] == [60.0] * (FAILS_BEFORE_SLOWING - 1)
    assert seen[2:6] == [120.0, 240.0, 480.0, 960.0] and seen[7] == cfg.source_backoff_max_min * 60.0
    fake.routes[LIVE] = lambda url, now: live_answer(now - timedelta(seconds=2))
    p.tick(clock.now())
    assert p.interval_at(clock.now()) == 60.0


def test_403_leg_dropped_for_the_day(setup, caplog) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    fake = FakeAlpaca(clock, **{DELAYED: FakeResponse(403, {}, b'{"code":42210000}')})
    p = poller(cfg, paths, clock, fake, log=logging.getLogger("t.drop"))
    with caplog.at_level(logging.WARNING, logger="t.drop"):
        first = p.poll(clock.now())
    assert first.legs[DELAYED].code == "http_403" and first.legs["closes"].code == "backoff"
    assert sum("not asked again until tomorrow" in r.getMessage() for r in caplog.records) == 1
    clock.advance(600)  # past the host's backoff
    second = p.poll(clock.now())
    assert second.legs[DELAYED].code == "dropped" and second.state == "recorded" and second.record["feed"] == "iex"
    assert fake.legs().count(DELAYED) == 1
    clock.set(et(date(2026, 11, 12), "10:00"))
    fake.prev_day = WED
    p.poll(clock.now())
    assert fake.legs().count(DELAYED) == 2  # asked again the next ET day


def test_refused_key_one_plain_line(setup, caplog) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    refused = FakeResponse(401, {}, b'{"message": "unauthorized."}')
    fake = FakeAlpaca(clock, **{LIVE: refused, DELAYED: refused, "closes": refused})
    p = poller(cfg, paths, clock, fake, log=logging.getLogger("t.key"))
    with caplog.at_level(logging.DEBUG, logger="t.key"):
        for _ in range(5):
            p.tick(clock.now())
            clock.advance(600)
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and r.name == "t.key"]
    assert msgs == ["Alpaca refused the key: check the two lines in Scout's .env"]
    assert {r["reason"] for r in lines(paths)} == {"refused_key"}
    assert p.interval_at(clock.now()) > cfg.reads.market_poll_s  # it keeps trying, slowed


def test_a_refused_shape_keeps_one_raw_copy_per_failure_state(setup) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    fake = FakeAlpaca(clock, **{LIVE: ok({"SPY": {"latestTrade": {"t": "x", "p": 1}}})})
    p = poller(cfg, paths, clock, fake, raw=RawStore(paths))
    for _ in range(4):
        p.tick(clock.now())
        clock.advance(60)
    kept = list((paths.raw / "alpaca_market").rglob("*"))
    assert len([k for k in kept if k.is_file()]) == 1


def test_tick_never_raises(setup, caplog) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))

    class Broken:
        def fetch(self, *a, **k):
            raise RuntimeError("boom")

    p = MarketPoller(cfg, paths, Broken(), KEY, clock, log=logging.getLogger("t.broken"))
    with caplog.at_level(logging.WARNING, logger="t.broken"):
        p.tick(clock.now())
        p.tick(clock.now())
    assert sum("failed unexpectedly" in r.getMessage() for r in caplog.records) == 1
    assert "boom" not in caplog.text


def test_slowing_is_capped_so_a_long_run_of_failures_never_overflows(setup, caplog) -> None:
    """Review L1: after about 1,026 failed polls `2 ** n` overflowed inside the scheduler's due()."""
    from scout.scheduler import EveryJob, run_due  # noqa: PLC0415

    cfg, paths = setup
    clock = ManualClock(et(WED, "10:00"))
    p = poller(cfg, paths, clock, FakeAlpaca(clock))
    ceiling = cfg.source_backoff_max_min * 60.0
    for fails in (FAILS_BEFORE_SLOWING + 20, 1025, 1026, 5000, 10 ** 9):
        p._fails = fails
        assert p.interval_at(clock.now()) == ceiling
    job = EveryJob("market", p.tick, 60, interval_fn=p.interval_at)
    job.last_run = clock.now() - timedelta(hours=5)
    with caplog.at_level(logging.DEBUG):
        assert run_due([job], clock.now(), logging.getLogger("t.due")) == ["market"]  # it ran
    assert "due() failed" not in caplog.text


def test_stale_polls_never_count_as_failures_and_reset_the_count(setup) -> None:
    """Review tests F2: pre-market stale polls must not slow the feed; a stale or recorded poll resets it."""
    cfg, paths = setup
    clock = ManualClock(et(WED, "05:00"))
    broken = FakeResponse(500, {}, b"")
    fake = FakeAlpaca(clock, **{LIVE: broken, DELAYED: broken})
    p = poller(cfg, paths, clock, fake)
    for _ in range(4):
        p.tick(clock.now())
        clock.advance(60)
    assert p._fails == 4 and p.interval_at(clock.now()) > cfg.reads.market_poll_s
    fake.routes[LIVE] = lambda url, now: live_answer(et(date(2026, 11, 10), "16:59"))  # yesterday's: only old
    fake.routes[DELAYED] = ok({"bars": {}, "next_page_token": None})
    for _ in range(10):
        p.tick(clock.now())
        clock.advance(60)
        assert p._fails == 0 and p.interval_at(clock.now()) == cfg.reads.market_poll_s
    assert {r["reason"] for r in lines(paths)[4:]} == {"stale"}
    fake.routes[LIVE] = fake.routes[DELAYED] = broken
    p.tick(clock.now())
    clock.set(et(WED, "10:00"))
    fake.routes[LIVE] = lambda url, now: live_answer(now - timedelta(seconds=2))
    fake.routes[DELAYED] = lambda url, now: delayed_answer(asked_end(url))
    p.tick(clock.now())
    assert lines(paths)[-1]["kind"] == "market" and p._fails == 0  # a recorded poll resets it too


def test_old_candidate_with_a_failing_request_is_a_failure(setup, caplog) -> None:
    """Review L4: 17:45 ET, IEX stopped at 17:00 (45 minutes old) and the whole-market request answers 500:
    a failed poll with that request's reason, not "stale"; no "before 08:00" wording outside the pre-market."""
    cfg, paths = setup
    clock = ManualClock(et(WED, "17:45"))
    fake = FakeAlpaca(clock, **{LIVE: lambda url, now: live_answer(et(WED, "17:00")),
                                DELAYED: FakeResponse(500, {}, b"oops")})
    p = poller(cfg, paths, clock, fake, log=logging.getLogger("t.post"))
    with caplog.at_level(logging.INFO, logger="t.post"):
        for _ in range(3):
            p.tick(clock.now())
            clock.advance(60)
    assert [r["reason"] for r in lines(paths)] == ["http_500"] * 3 and lines(paths)[0]["legs"][DELAYED] == "http_500"
    assert p._fails == 3
    assert "no usable answer" in caplog.text and "08:00" not in caplog.text
    # both requests answered and both are only old: stale, and the log line says nothing of 08:00 after the open
    caplog.clear()
    fake.routes[DELAYED] = lambda url, now: delayed_answer(now - timedelta(minutes=50))
    with caplog.at_level(logging.INFO, logger="t.post"):
        out = p.poll(clock.now())
    assert out.state == "stale" and out.reason == "stale" and p._fails == 0
    assert "older than 30 minutes" in caplog.text and "08:00" not in caplog.text


def test_a_dropped_request_does_not_turn_old_prices_into_a_failure(setup) -> None:
    """One edge of the rule above: the whole-market request was refused beside an answer and is left out
    for the day; at 17:45 the live prices are only old. Nothing was sent and failed in that poll, so it is
    stale, not failed, and the feed does not slow."""
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    fake = FakeAlpaca(clock, **{DELAYED: FakeResponse(403, {}, b'{"message": "no"}')})
    p = poller(cfg, paths, clock, fake)
    p.poll(clock.now())  # the 403 beside the live answer: left out for the day (and the host backs off)
    clock.set(et(WED, "10:10"))
    assert p.poll(clock.now()).state == "recorded"
    clock.set(et(WED, "17:45"))
    fake.routes[LIVE] = lambda url, now: live_answer(et(WED, "17:00"))
    out = p.poll(clock.now())
    assert out.legs[DELAYED].code == "dropped"
    assert (out.state, out.reason) == ("stale", "stale") and p._fails == 0


def test_an_answer_without_a_bar_beside_old_prices_is_stale(setup) -> None:
    """The other edge: the whole-market request answered, with no bar, while the live prices are only old.
    Both answered, so the poll is stale, never a failure with the reason "ok"."""
    cfg, paths = setup
    clock = ManualClock(et(WED, "17:45"))
    fake = FakeAlpaca(clock, **{LIVE: lambda url, now: live_answer(et(WED, "17:00")),
                                DELAYED: ok({"bars": {}, "next_page_token": None})})
    p = poller(cfg, paths, clock, fake)
    out = p.poll(clock.now())
    assert (out.state, out.reason) == ("stale", "stale") and p._fails == 0


def test_stale_log_mentions_the_early_hours_only_in_the_pre_market(setup, caplog) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "05:00"))
    fake = FakeAlpaca(clock, **{LIVE: lambda url, now: live_answer(now - timedelta(minutes=40)),
                                DELAYED: lambda url, now: delayed_answer(now - timedelta(minutes=45))})
    p = poller(cfg, paths, clock, fake, log=logging.getLogger("t.pre"))
    with caplog.at_level(logging.INFO, logger="t.pre"):
        assert p.poll(clock.now()).state == "stale"
    assert "(normal before about 08:00 ET)" in caplog.text


def test_a_pc_clock_a_little_slow_still_shows_live_prices(setup) -> None:
    """Review L5: a trade up to 60 s ahead of Scout's clock is accepted (a Windows clock can drift)."""
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:00"))
    fake = FakeAlpaca(clock, **{LIVE: lambda url, now: live_answer(now + timedelta(seconds=13))})
    out = poller(cfg, paths, clock, fake).poll(clock.now())
    assert out.state == "recorded" and out.record["feed"] == "iex" and LIVE not in out.problems
    fake.routes[LIVE] = lambda url, now: live_answer(now + timedelta(seconds=75))
    out = poller(cfg, paths, clock, fake).poll(clock.now())
    assert out.problems[LIVE] == "future_time" and out.chosen["feed"] == DELAYED


@pytest.mark.parametrize("failure", [socket.timeout("slow"), OSError("reset")])
@pytest.mark.parametrize("where", [LIVE, DELAYED])
def test_a_timeout_or_network_failure_on_any_request_ends_the_poll(setup, where: str, failure: Exception) -> None:
    """Review secrets F1 and tests F7: no further request is sent in that poll (an OSError on the live
    request ends it like a timeout)."""
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    fake = FakeAlpaca(clock, **{where: failure})
    out = poller(cfg, paths, clock, fake).poll(clock.now())
    asked = [LIVE] if where == LIVE else [LIVE, DELAYED]
    assert fake.legs() == asked
    code = "timeout" if isinstance(failure, socket.timeout) else "network"
    assert out.legs[where].code == code
    assert all(out.legs[n].code == "skipped" for n in (LIVE, DELAYED, "closes") if n not in asked)
    if where == DELAYED:  # the live prices came, but without the closes there is no record
        assert out.state == "failed" and out.record is None


def test_timeout_s_is_capped_at_ten(scout_dir: Path) -> None:
    from scout.config import ConfigError  # noqa: PLC0415

    assert make_config(scout_dir, reads_market_timeout_s=10).reads.market_timeout_s == 10.0
    with pytest.raises(ConfigError, match="reads_market_timeout_s"):
        make_config(scout_dir, reads_market_timeout_s=11)


def test_every_request_403_drops_nothing_and_says_the_key_is_refused(setup, caplog) -> None:
    """Review tests F4: the drop rule needs another request of the same poll answered 200."""
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    refused = FakeResponse(403, {}, b'{"message": "forbidden"}')
    fake = FakeAlpaca(clock, **{LIVE: refused, DELAYED: refused, "closes": refused})
    p = poller(cfg, paths, clock, fake, log=logging.getLogger("t.403"))
    with caplog.at_level(logging.WARNING, logger="t.403"):
        out = p.poll(clock.now())
    assert out.reason == "refused_key" and fake.legs() == [LIVE] and p._dropped == set()
    assert "not asked again until tomorrow" not in caplog.text
    assert "Alpaca refused the key: check the two lines in Scout's .env" in caplog.text
    clock.advance(3600)  # past the host's backoff
    p.poll(clock.now())
    assert fake.legs().count(LIVE) == 2 and p._dropped == set()  # asked again later


def test_422_while_another_request_answered_drops_that_request(setup, caplog) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    fake = FakeAlpaca(clock, **{DELAYED: FakeResponse(422, {}, b'{"message": "invalid"}')})
    p = poller(cfg, paths, clock, fake, log=logging.getLogger("t.422"))
    with caplog.at_level(logging.WARNING, logger="t.422"):
        first = p.poll(clock.now())
    assert first.legs[DELAYED].code == "http_422" and first.state == "recorded" and p._dropped == {DELAYED}
    assert fake.legs() == [LIVE, DELAYED, "closes"]  # a 422 does not back the host off
    assert sum("not asked again until tomorrow" in r.getMessage() for r in caplog.records) == 1
    clock.advance(60)
    assert p.poll(clock.now()).legs[DELAYED].code == "dropped" and fake.legs().count(DELAYED) == 1


def test_a_miss_line_only_when_a_request_was_sent(setup) -> None:
    """Review tests F6: a poll that sent nothing (the host backing off) writes no miss line."""
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    fake = FakeAlpaca(clock, **{LIVE: FakeResponse(429, {}, b"")})
    p = poller(cfg, paths, clock, fake)
    p.tick(clock.now())
    assert [(r["kind"], r["reason"]) for r in lines(paths)] == [("miss", "http_429")]
    for _ in range(3):
        clock.advance(10)
        n = len(fake.calls)
        p.tick(clock.now())
        assert len(fake.calls) == n  # backing off: nothing sent
    assert len(lines(paths)) == 1


def test_poller_repr_hides_the_key(setup) -> None:
    cfg, paths = setup
    clock = ManualClock(et(WED, "10:02"))
    p = poller(cfg, paths, clock, FakeAlpaca(clock))
    assert FAKE_ID not in repr(p) and FAKE_SECRET not in repr(p) and FAKE_ID not in repr(p.fetcher)
