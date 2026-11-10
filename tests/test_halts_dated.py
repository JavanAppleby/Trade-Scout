"""The evening dated halt read, `&haltdate=MMDDYYYY` (SCOUT_SPEC 4.1 #6, 6.3, 7.5, 7.8, 12; DESIGN 2.3, 23).

The dated feed, read live on 2026-11-10, pads HaltTime between the seconds and the fraction
("09:30:17" + 22 spaces + ".816" where the live feed has "09:30:17.816") and names the market `Mkt`.
Before 0.3.1 every dated row was refused for that padding, and the read was tried every 60 s until
midnight ET. `tradehalts_dated_SYNTHETIC.xml` is hand-built in its shape (fixtures README)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from helpers_fakes import FakeBoard, FakeFactSink, FakeFetcher, FakeHealthSink, FakeRawSink
from helpers_macro import HALTS_URL, halts_body
from scout_reader.halts import open_halts

from scout.halts import FEED_DATED, FEED_LIVE, HaltPoller, dated_url, parse_halts, parse_halts_rows
from scout.jobs_macro import halts_tick

UTC = timezone.utc
DATED = "tradehalts_dated_SYNTHETIC.xml"
DAY = datetime(2026, 11, 2).date()
DATED_URL = dated_url(HALTS_URL, DAY)
PAD = " " * 22  # the dated feed's padding after the seconds
ABCD_DATED = "09:31:05" + PAD + ".123"
LATE_DATED = "19:50:00" + PAD + ".000"


def _by_symbol(body: bytes) -> dict:
    return {r.symbol: r for r in parse_halts(body)}


def _poller(cfg, clock, results: dict):  # noqa: ANN001, ANN202
    fetcher = FakeFetcher(results)
    sinks = (FakeRawSink(), FakeFactSink(), FakeHealthSink(), FakeBoard())
    return HaltPoller(cfg, fetcher, *sinks, clock), fetcher, sinks


# --------------------------------------------------------------------------- parser


def test_every_dated_row_is_read() -> None:
    rows, skipped = parse_halts_rows(halts_body(DATED))
    assert skipped == []
    assert sorted(r.symbol for r in rows) == ["ABCD", "ARCA", "LATE", "MISS", "NEWS", "PADS", "WXYZ"]


def test_a_dated_row_has_the_live_rows_key_times_and_resumption() -> None:
    """The halts both fixtures list (2 Nov 2026) get one key: the padding is left out of it."""
    dated, live = _by_symbol(halts_body(DATED)), _by_symbol(halts_body())
    for sym in ("ABCD", "WXYZ", "NEWS", "ARCA"):
        assert dated[sym].key == live[sym].key, sym
        assert (dated[sym].halt_at, dated[sym].resumed_at) == (live[sym].halt_at, live[sym].resumed_at), sym
    assert dated["ABCD"].halt_time == "09:31:05.123"
    assert dated["ABCD"].halt_at == datetime(2026, 11, 2, 14, 31, 5, 123000, tzinfo=UTC)
    assert dated["WXYZ"].halt_time == "09:45:00"  # padding and no fraction
    assert dated["LATE"].key == "LATE|11/02/2026|19:50:00.000" and dated["LATE"].resumed_at is None


def test_whitespace_around_every_field_is_stripped_before_matching() -> None:
    pads = _by_symbol(halts_body(DATED))["PADS"]
    assert (pads.symbol, pads.halt_date, pads.halt_time, pads.reason, pads.market, pads.issue_name) == (
        "PADS", "11/02/2026", "10:12:13.456", "LUDP", "Q", "Padded Fields Corp")
    assert pads.halt_at == datetime(2026, 11, 2, 15, 12, 13, 456000, tzinfo=UTC)
    assert pads.resumed_at == datetime(2026, 11, 2, 15, 17, 13, tzinfo=UTC)
    assert pads.key == "PADS|11/02/2026|10:12:13.456"


def test_the_row_is_kept_as_received_padding_included() -> None:
    dated = _by_symbol(halts_body(DATED))
    assert dated["ABCD"].row["HaltTime"] == ABCD_DATED
    assert dated["PADS"].row["IssueSymbol"] == " PADS  " and dated["PADS"].row["Mkt"] == " Q "
    assert dated["NEWS"].row["ResumptionTradeTime"] == "10:30:00" + PAD
    assert dated["LATE"].row["ResumptionTradeTime"] == ""  # <ndaq:ResumptionTradeTime />
    assert "Market" not in dated["ABCD"].row and dated["ABCD"].row["Mkt"] == "Q"


def test_the_dated_feeds_mkt_is_the_market() -> None:
    assert {s: r.market for s, r in _by_symbol(halts_body(DATED)).items()} == {
        "NEWS": "Q", "ABCD": "Q", "WXYZ": "N", "ARCA": "P", "PADS": "Q", "MISS": "Q", "LATE": "Q"}


@pytest.mark.parametrize("bad", [
    "09:31:05" + PAD + "123",  # no point before the fraction
    "09:31:05" + PAD + ". 123",
    "09:31:05" + PAD + ".123 x",
    "09:31 :05.123",
    "09:31:05.123.4",
    "9:31",
])
def test_spaces_are_allowed_only_before_the_fraction(bad: str) -> None:
    rows, skipped = parse_halts_rows(halts_body(DATED).replace(ABCD_DATED.encode(), bad.encode(), 1))
    assert len(rows) == 6 and len(skipped) == 1 and "unreadable time" in skipped[0]


def test_the_error_shows_the_time_past_the_padding() -> None:
    """2-9 Nov 2026 the message cut the time at 20 characters, which hid the fraction."""
    _rows, skipped = parse_halts_rows(halts_body(DATED).replace(ABCD_DATED.encode(), b"09:31:05" + PAD.encode() + b"x123", 1))
    assert "x123" in skipped[0]


# --------------------------------------------------------------------------- the evening read


def test_the_dated_read_adds_only_what_the_live_polls_missed(cfg, clock, et) -> None:  # noqa: ANN001
    """The live polls saw NEWS, ABCD, WXYZ and ARCA: the dated read adds nothing for them. MISS and
    PADS ended unseen and are recorded with their resumptions. LATE is still open: left to the live
    feed, which lists an open halt until it resumes."""
    poller, fetcher, (raw, facts, health, board) = _poller(cfg, clock, {HALTS_URL: halts_body(),
                                                                       DATED_URL: halts_body(DATED)})
    halts_tick(poller, cfg, et("2026-11-02", "19:59"))
    seen_live = len(facts.facts)
    halts_tick(poller, cfg, et("2026-11-02", "20:01"))
    assert fetcher.calls[-1][1] == DATED_URL and poller.dated_done == DAY
    new = facts.facts[seen_live:]
    assert sorted(f.ticker for f in new) == ["MISS", "PADS"]
    assert {f.feed for f in new} == {FEED_DATED} and all(f.resumed_at for f in new)
    pads = next(f for f in new if f.ticker == "PADS")
    assert pads.guid == "PADS|11/02/2026|10:12:13.456" and pads.halt_market == "Q"
    assert pads.halt_time == "2026-11-02T15:12:13.456000Z" and pads.resumed_at == "2026-11-02T15:17:13Z"
    assert pads.extra["row"]["IssueSymbol"] == " PADS  "  # stored as received
    rec = health.records[-1]
    assert (rec.feed, rec.result, rec.items, rec.new_items, rec.error) == (FEED_DATED, "ok", 7, 2, None)
    assert board.sources["nasdaq_halts"].status == "ok" and board.sources["nasdaq_halts"].gap_open is False


def test_after_a_restart_the_dated_rows_join_the_live_keys(cfg, clock, et) -> None:  # noqa: ANN001
    """The same fact store, a fresh poller (a restart after the live polls): every dated fact of a
    halt on file carries the live key, so the halt keeps one key; only MISS and PADS are new."""
    facts = FakeFactSink()
    HaltPoller(cfg, FakeFetcher({HALTS_URL: halts_body()}), FakeRawSink(), facts, FakeHealthSink(), FakeBoard(),
               clock).poll(et("2026-11-02", "19:59"))
    live_keys = {f.extra["halt_key"] for f in facts.facts}
    restarted = HaltPoller(cfg, FakeFetcher({DATED_URL: halts_body(DATED)}), FakeRawSink(), facts, FakeHealthSink(),
                           FakeBoard(), clock)
    restarted.dated_read(et("2026-11-02", "20:30"))
    dated = [f for f in facts.facts if f.feed == FEED_DATED]
    assert {f.extra["halt_key"] for f in dated} - live_keys == {"MISS|11/02/2026|11:15:20.250",
                                                                "PADS|11/02/2026|10:12:13.456"}
    assert all(f.resumed_at for f in dated)  # resumptions on the live keys, and the two halts that ended


def _late(resumed: bool) -> bytes:
    """A live-feed item: LATE halted at 19:50 ET on 2 Nov (news pending), resumed or not at 09:30 the
    next morning."""
    when = ("11/03/2026", "09:25:00", "09:30:00") if resumed else ("", "", "")
    return ("<item>\n<title>LATE</title>\n<ndaq:HaltDate>11/02/2026</ndaq:HaltDate>\n"
            "<ndaq:HaltTime>19:50:00.000</ndaq:HaltTime>\n<ndaq:IssueSymbol>LATE</ndaq:IssueSymbol>\n"
            "<ndaq:IssueName>Late News Inc</ndaq:IssueName>\n<ndaq:Market>NASDAQ</ndaq:Market>\n"
            "<ndaq:ReasonCode>T1</ndaq:ReasonCode>\n<ndaq:PauseThresholdPrice></ndaq:PauseThresholdPrice>\n"
            f"<ndaq:ResumptionDate>{when[0]}</ndaq:ResumptionDate>\n"
            f"<ndaq:ResumptionQuoteTime>{when[1]}</ndaq:ResumptionQuoteTime>\n"
            f"<ndaq:ResumptionTradeTime>{when[2]}</ndaq:ResumptionTradeTime>\n</item>\n").encode()


def _live_with(item: bytes) -> bytes:
    return halts_body().replace(b"</channel>", item + b"</channel>", 1)


@pytest.mark.parametrize("dated_time", [LATE_DATED, "19:50:00" + PAD])  # the live key; another key
def test_an_evening_row_never_keeps_a_resumed_halt_open(cfg, clock, et, dated_time: str) -> None:  # noqa: ANN001
    """The false block 0.3.1 rules out: a halt open at 20:00 ET that resumes the next morning. The
    live feed records it and its resumption under the live key. A dated copy under another key (here
    its time without the fraction) would never see that resumption and would block the stock for up
    to 3 trading days (`halt_unresolved_since`). The dated read stores no open row, whatever its key."""
    dated_body = halts_body(DATED).replace(LATE_DATED.encode(), dated_time.encode())
    assert {r.key for r in parse_halts(dated_body) if r.symbol == "LATE"} == {
        "LATE|11/02/2026|" + "".join(dated_time.split())}
    poller, fetcher, (_r, facts, _h, _b) = _poller(cfg, clock, {HALTS_URL: _live_with(_late(False)),
                                                               DATED_URL: dated_body})
    halts_tick(poller, cfg, et("2026-11-02", "19:55"))  # live: LATE halted at 19:50, open
    halts_tick(poller, cfg, et("2026-11-02", "20:01"))  # dated: LATE still open there
    assert poller.dated_done == DAY and not [f for f in facts.facts if f.ticker == "LATE" and f.feed == FEED_DATED]
    fetcher.results[HALTS_URL] = _live_with(_late(True))
    halts_tick(poller, cfg, et("2026-11-03", "09:31"))  # live, next morning: resumed at 09:30
    records = [f.to_record() for f in facts.facts]
    today, older = open_halts(records, "LATE", et("2026-11-02", "20:30"))
    assert [k for k, _recs in today] == ["LATE|11/02/2026|19:50:00.000"] and older == []  # open, one key
    for as_of in (et("2026-11-03", "09:45"), et("2026-11-04", "10:00"), et("2026-11-05", "15:00")):
        assert open_halts(records, "LATE", as_of) == ([], []), as_of
    assert {f.feed for f in facts.facts if f.ticker == "LATE"} == {FEED_LIVE}


# --------------------------------------------------------------------------- retries


def test_a_failing_dated_read_is_tried_every_15_minutes_not_every_minute(cfg, clock, et) -> None:  # noqa: ANN001
    """Before 0.3.1 a failing dated read was tried every 60 s from 20:00 ET to midnight: about 235
    requests a night on every trading day of 2-9 Nov 2026. Now every `halts_dated_retry_s` (900 s):
    16 a night at most."""
    poller, fetcher, _sinks = _poller(cfg, clock, {DATED_URL: b"<rss><nope/></rss>"})
    start = et("2026-11-02", "20:00")
    sent = []
    for s in range(0, 4 * 3600 + 600, 5):  # the job asks every second; every 5 s is enough here
        before = len(fetcher.calls)
        halts_tick(poller, cfg, start + timedelta(seconds=s))
        if len(fetcher.calls) > before:
            sent.append(s)
    assert sent == [900 * n for n in range(16)]  # 20:00, 20:15, ..., 23:45; nothing after midnight ET
    assert {url for _src, url in fetcher.calls} == {DATED_URL} and poller.dated_done is None


def test_a_dated_retry_never_comes_sooner_than_the_60_s_minimum(cfg, clock, et) -> None:  # noqa: ANN001
    """The lowest retry config.toml allows is 60 s (below it is refused, test_config); at 60 s the
    retry keeps the halt feed's hard minimum."""
    poller, fetcher, _sinks = _poller(replace(cfg, halts_dated_retry_s=60), clock, {DATED_URL: b"<rss><nope/></rss>"})
    assert poller.dated_retry_s == 60.0
    poller.dated_read(et("2026-11-02", "20:01"))
    poller.dated_read(et("2026-11-02", "20:01") + timedelta(seconds=59))
    assert len(fetcher.calls) == 1
    poller.dated_read(et("2026-11-02", "20:02"))
    assert len(fetcher.calls) == 2


def test_a_clock_stepped_back_does_not_stop_the_dated_retry(cfg, clock, et) -> None:  # noqa: ANN001
    poller, fetcher, _sinks = _poller(cfg, clock, {DATED_URL: b"<rss><nope/></rss>"})
    poller.dated_read(et("2026-11-02", "21:00"))
    poller.dated_read(et("2026-11-02", "20:30"))  # the wall clock stepped back half an hour
    assert len(fetcher.calls) == 2
