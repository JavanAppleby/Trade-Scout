"""Scout's Alpaca parsers and market feed on REAL answers (0.3.2; SCOUT_SPEC 19.5): the bodies the owner's
probe saved on 10 Nov 2026 at 20:02 UTC (`tests/fixtures/alpaca/README.md`). No network, no key: the
fake transport hands the saved bodies back."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from conftest import make_config
from helpers_fakes import FakeResponse
from helpers_market import FakeAlpaca, poller

from scout.alpaca_data import parse_closes, parse_minute_bars, parse_snapshots, parse_time
from scout.calendar import pre_open, previous_trading_day
from scout.clock import ManualClock, et_date
from scout.market_feed import CLOSES, DELAYED, LIVE, candidate, choose, market_day, market_record
from scout.paths import ScoutPaths
from scout.reads_market import market_line, market_lines, shown_market

ALPACA = Path(__file__).resolve().parent / "fixtures" / "alpaca"
SYMS = ("SPY", "QQQ", "IWM")
ASKED = datetime(2026, 11, 10, 20, 2, 36, tzinfo=timezone.utc)  # the live snapshot was asked then
NOW = ASKED + timedelta(seconds=1)
TUE, MON = date(2026, 11, 10), date(2026, 11, 9)
LINE = "Market at 15:02:18 ET: SPY 748.36 -0.58%; QQQ 671.91 -1.10%; IWM 279.88 -0.93%"


def body(name: str) -> bytes:
    return (ALPACA / name).read_bytes()


def answer(name: str, status: int = 200) -> FakeResponse:
    return FakeResponse(status, {"content-type": "application/json"}, body(name))


def test_the_parsers_read_the_real_answers() -> None:
    live = parse_snapshots(body("snapshots_iex.json"), SYMS)
    assert {s: p.text for s, p in live.items()} == {"SPY": "748.355", "QQQ": "671.91", "IWM": "279.88"}
    assert live["IWM"].trade_at == datetime(2026, 11, 10, 20, 2, 18, 185917, tzinfo=timezone.utc)  # 9 digits cut to 6
    delayed = parse_snapshots(body("snapshots_delayed_sip.json"), SYMS)
    # delayed_sip trades are about 15 minutes old (19:47:42 against 20:02:37 asked)
    assert set(delayed) == set(SYMS) and all(14 * 60 < (ASKED - p.trade_at).total_seconds() < 16 * 60
                                             for p in delayed.values())
    minute = parse_minute_bars(body("bars_1min_iex.json"), SYMS)
    assert {s: p.text for s, p in minute.items()} == {"SPY": "748.415", "QQQ": "671.89", "IWM": "279.87"}
    assert all(p.trade_at == datetime(2026, 11, 10, 20, 2, tzinfo=timezone.utc) for p in minute.values())  # 20:01 bar


def test_the_previous_closes_are_the_whole_markets_9_november_closes() -> None:
    assert market_day(NOW) == TUE and previous_trading_day(TUE) == MON
    closes = parse_closes(body("bars_1day_sip.json"), SYMS, MON)
    assert closes == {"SPY": Decimal("752.69"), "QQQ": Decimal("679.35"), "IWM": Decimal("282.51")}
    # the same closes as the delayed snapshot's prevDailyBar (never read by Scout; a cross-check only)
    snap = json.loads(body("snapshots_delayed_sip.json"))
    assert {s: Decimal(str(snap[s]["prevDailyBar"]["c"])) for s in SYMS} == closes


def test_the_record_and_the_readers_line_from_the_real_answers() -> None:
    live = parse_snapshots(body("snapshots_iex.json"), SYMS)
    closes = parse_closes(body("bars_1day_sip.json"), SYMS, MON)
    cand, why = candidate(LIVE, live, closes, SYMS, NOW, pre_open(TUE))
    assert why == ""
    rec = market_record(choose([cand]), NOW, TUE, MON)
    assert rec["feed"] == "iex" and rec["at"] == "2026-11-10T20:02:18.185917Z"  # the oldest trade (IWM)
    assert {s: v["change_pct"] for s, v in rec["symbols"].items()} == {
        "SPY": "-0.005759", "QQQ": "-0.010952", "IWM": "-0.009309"}
    assert {s: v["prev_close"] for s, v in rec["symbols"].items()} == {"SPY": "752.69", "QQQ": "679.35",
                                                                         "IWM": "282.51"}
    assert rec["symbols"]["SPY"]["last"] == "748.355"
    shown = shown_market(None, rec, NOW, 1800.0, SYMS)
    assert market_line(shown, NOW, et_date(NOW)) == LINE


@pytest.fixture
def setup(scout_dir: Path):
    cfg = make_config(scout_dir, reads_enabled=True)
    return cfg, ScoutPaths.from_config(cfg)


def test_a_whole_poll_on_the_real_answers_records_the_same_line(setup) -> None:
    cfg, paths = setup
    clock = ManualClock(NOW)
    fake = FakeAlpaca(clock, prev_day=MON, **{LIVE: answer("snapshots_iex.json"),
                                              DELAYED: answer("bars_1min_iex.json"),  # the bars shape
                                              CLOSES: answer("bars_1day_sip.json")})
    p = poller(cfg, paths, clock, fake)
    p.tick(clock.now())
    recs = market_lines(paths, TUE)
    assert [r["kind"] for r in recs] == ["market"]
    rec = recs[0]
    assert rec["feed"] == "iex" and rec["at"] == "2026-11-10T20:02:18.185917Z" and rec["prev_day"] == "2026-11-09"
    assert {s: v["change_pct"] for s, v in rec["symbols"].items()} == {
        "SPY": "-0.005759", "QQQ": "-0.010952", "IWM": "-0.009309"}
    t = parse_time(rec["first_seen_at"])
    assert market_line(shown_market(None, rec, t, cfg.reads.market_max_age_s, SYMS), t, et_date(t)) == LINE


def test_the_real_recent_sip_refusal_drops_that_request_only(setup, caplog) -> None:
    cfg, paths = setup
    clock = ManualClock(NOW)
    refused = answer("refused_recent_sip_403.json", 403)
    assert json.loads(refused.body) == {"message": "subscription does not permit querying recent SIP data"}
    fake = FakeAlpaca(clock, prev_day=MON, **{LIVE: answer("snapshots_iex.json"),
                                              DELAYED: answer("bars_1min_iex.json"),
                                              CLOSES: answer("bars_1day_sip.json")})
    p = poller(cfg, paths, clock, fake, log=logging.getLogger("t.real"))
    p.tick(clock.now())  # the closes are now held for the day
    fake.routes[DELAYED] = refused
    with caplog.at_level(logging.WARNING, logger="t.real"):
        clock.advance(60)
        p.tick(clock.now())
        clock.advance(600)  # past the host's backoff
        out = p.poll(clock.now())
    assert out.legs[DELAYED].code == "dropped" and fake.legs().count(DELAYED) == 2
    assert sum("not asked again until tomorrow" in r.getMessage() for r in caplog.records) == 1
    text = (paths.reads / "market" / "2026-11-10.jsonl").read_text(encoding="utf-8")
    assert "subscription" not in text and "SIP data" not in caplog.text  # codes only, never body text
