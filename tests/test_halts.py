"""Halt feed parser and poller (SCOUT_SPEC 4.1 #6, 6.3, 7.5, 14; DESIGN 2.3)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from helpers_fakes import FakeBoard, FakeFactSink, FakeFetcher, FakeHealthSink, FakeRawSink, fetch_result
from helpers_macro import HALTS_URL, halts_body
from scout_reader import blocks as reader_blocks

from scout.halts import (FEED_DATED, FEED_LIVE, HARD_MIN_SPACING_S, HaltPoller, dated_url, halt_key, parse_halts,
                         parse_halts_rows)
from scout.jobs_macro import halts_tick
from scout.macro import SchemaError, fact_id

UTC = timezone.utc


def _rows() -> dict:
    return {r.symbol: r for r in parse_halts(halts_body())}


def _swap(old: bytes, new: bytes, body: bytes | None = None) -> bytes:
    body = body if body is not None else halts_body()
    assert old in body
    return body.replace(old, new, 1)


# --------------------------------------------------------------------------- parser


def test_parses_every_row_with_bom_and_namespace() -> None:
    body = halts_body()
    assert body.startswith(b"\xef\xbb\xbf")
    rows = _rows()
    assert sorted(rows) == ["ABCD", "ARCA", "NEWS", "OLDC", "WXYZ"]


def test_halt_time_is_et_converted_once_with_fractional_seconds() -> None:
    rows = _rows()
    assert rows["ABCD"].halt_at == datetime(2026, 11, 2, 14, 31, 5, 123000, tzinfo=UTC)  # 09:31:05.123 EST
    assert rows["ARCA"].halt_at == datetime(2026, 11, 2, 14, 58, 30, 100000, tzinfo=UTC)
    assert rows["OLDC"].halt_at == datetime(2019, 4, 16, 20, 15, 1, tzinfo=UTC)  # EDT in April
    assert rows["ABCD"].halt_time == "09:31:05.123"  # as received


def test_market_is_nasdaq_or_one_letter() -> None:
    rows = _rows()
    assert {s: r.market for s, r in rows.items()} == {"ABCD": "NASDAQ", "ARCA": "P", "NEWS": "Q", "OLDC": "NASDAQ",
                                                      "WXYZ": "N"}


def test_empty_resumption_means_still_halted_and_codes_kept() -> None:
    rows = _rows()
    assert rows["WXYZ"].resumed_at is None and rows["WXYZ"].reason == "M"
    assert rows["OLDC"].resumed_at is None and rows["OLDC"].reason == "T12"
    assert rows["ABCD"].resumed_at == datetime(2026, 11, 2, 14, 36, 5, tzinfo=UTC)
    assert rows["ABCD"].reason == "LUDP"
    assert rows["NEWS"].resumed_at == datetime(2026, 11, 2, 15, 30, tzinfo=UTC)
    # no ResumptionDate: the halt date is used
    assert rows["ARCA"].resumed_at == datetime(2026, 11, 2, 15, 3, 30, tzinfo=UTC)


def test_rows_are_kept_as_received() -> None:
    row = _rows()["ABCD"].row
    assert row["PauseThresholdPrice"] == "1.2300"
    assert row["IssueName"] == "Abcd Therapeutics Inc"
    assert row["ResumptionQuoteTime"] == "09:36:05"


def test_pubdate_is_ignored() -> None:
    a = parse_halts(halts_body())
    b = parse_halts(halts_body().replace(b"Mon, 02 Nov 2026 15:00:00 GMT", b"not a date at all"))
    assert a == b


def test_no_order_is_assumed() -> None:
    body = halts_body()
    head, _, rest = body.partition(b"<item>")
    items = [b"<item>" + chunk for chunk in rest.split(b"<item>")]
    items[-1], tail = items[-1].split(b"</channel>")
    reversed_body = head + b"".join(reversed(items)) + b"</channel>" + tail
    assert sorted(parse_halts(reversed_body), key=lambda r: r.symbol) == sorted(parse_halts(body),
                                                                                  key=lambda r: r.symbol)


def test_without_bom_and_empty_feed() -> None:
    assert len(parse_halts(halts_body()[3:])) == 5
    assert parse_halts(halts_body("tradehalts_empty_SYNTHETIC.xml")) == []


@pytest.mark.parametrize("market,stored", [
    (b"<ndaq:Market>NYSE</ndaq:Market>", "NYSE"), (b"<ndaq:Market>AMEX</ndaq:Market>", "AMEX"),
    (b"<ndaq:Market>Non NASDAQ</ndaq:Market>", "Non NASDAQ"), (b"<ndaq:Market></ndaq:Market>", None), (b"", None),
])
def test_any_market_text_or_none_is_accepted_and_stored(market: bytes, stored: str | None) -> None:
    """The forms the live feed sent on 2 and 3 Nov 2026 (owner, 2026-11-04): stored as received."""
    rows = {r.symbol: r for r in parse_halts(_swap(b"<ndaq:Market>N</ndaq:Market>", market))}
    assert len(rows) == 5 and rows["WXYZ"].market == stored
    assert ("Market" in rows["WXYZ"].row) == bool(market)


@pytest.mark.parametrize("old,new", [
    (b"<ndaq:ResumptionTradeTime></ndaq:ResumptionTradeTime>", b""),  # a required field missing
    (b"<ndaq:HaltTime>09:45:00</ndaq:HaltTime>", b"<ndaq:HaltTime>9:45</ndaq:HaltTime>"),
    (b"<ndaq:HaltDate>04/16/2019</ndaq:HaltDate>", b"<ndaq:HaltDate>2019-04-16</ndaq:HaltDate>"),
    (b"<ndaq:HaltDate>04/16/2019</ndaq:HaltDate>", b"<ndaq:HaltDate>13/45/2019</ndaq:HaltDate>"),
    (b"<ndaq:IssueSymbol>WXYZ</ndaq:IssueSymbol>", b"<ndaq:IssueSymbol> </ndaq:IssueSymbol>"),
    (b"<ndaq:ResumptionTradeTime>10:30:00</ndaq:ResumptionTradeTime>",
     b"<ndaq:ResumptionTradeTime>soon</ndaq:ResumptionTradeTime>"),
])
def test_a_bad_row_is_skipped_and_counted_never_the_whole_feed(old: bytes, new: bytes) -> None:
    rows, skipped = parse_halts_rows(_swap(old, new))
    assert len(rows) == 4 and len(skipped) == 1 and skipped[0].startswith("halts item ")
    assert len(parse_halts(_swap(old, new))) == 4


def test_every_row_bad_is_feed_drift() -> None:
    body = halts_body().replace(b"<ndaq:HaltDate>", b"<ndaq:Nope>").replace(b"</ndaq:HaltDate>", b"</ndaq:Nope>")
    with pytest.raises(SchemaError, match="no readable row"):
        parse_halts(body)


@pytest.mark.parametrize("body", [
    b"", b"not xml", b"<html><body>blocked</body></html>",
    b'<?xml version="1.0"?><rss version="2.0"></rss>',  # no channel
    b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "x">]><rss><channel/></rss>',
])
def test_not_a_halt_feed_is_schema_drift(body: bytes) -> None:
    with pytest.raises(SchemaError):
        parse_halts(body)


def test_un_namespaced_fields_are_not_read() -> None:
    body = halts_body().replace(b"ndaq:", b"").replace(b' xmlns:="http://www.nasdaqtrader.com/"', b"")
    body = body.replace(b' xmlns:ndaq="http://www.nasdaqtrader.com/"', b"")
    with pytest.raises(SchemaError):
        parse_halts(body)


# --------------------------------------------------------------------------- poller


def _poller(cfg, clock, body: bytes | None = None, results: dict | None = None):
    fetcher = FakeFetcher(results if results is not None else {HALTS_URL: body if body is not None else halts_body()})
    sinks = (FakeRawSink(), FakeFactSink(), FakeHealthSink(), FakeBoard())
    poller = HaltPoller(cfg, fetcher, *sinks, clock)
    return poller, fetcher, sinks


def test_poll_writes_one_halt_fact_per_row(cfg, clock, et) -> None:
    now = et("2026-11-02", "10:00")
    clock.set(now)
    poller, fetcher, (raw, facts, health, board) = _poller(cfg, clock)
    assert poller.poll(now) == 5
    by = {f.ticker: f for f in facts.facts}
    f = by["ABCD"]
    assert f.kind == "halt" and f.source == "nasdaq_halts" and f.feed == FEED_LIVE
    assert f.guid == halt_key("ABCD", "11/02/2026", "09:31:05.123")
    assert f.fact_id == fact_id("nasdaq_halts", f.guid, "ABCD")
    assert f.halt_time == "2026-11-02T14:31:05.123000Z" and f.published_at == f.halt_time
    assert f.resumed_at == "2026-11-02T14:36:05Z"
    assert f.halt_code == "LUDP" and f.halt_market == "NASDAQ"
    assert f.first_seen_at == "2026-11-02T15:00:00Z"
    assert f.extra["halt_key"] == f.guid and f.extra["row"]["Market"] == "NASDAQ"
    assert f.raw_sha256 is not None and raw.saved and raw.saved[0][0] == "nasdaq_halts"
    assert by["WXYZ"].resumed_at is None
    assert board.sources["nasdaq_halts"].status == "ok"
    assert health.records[-1].result == "ok" and health.records[-1].items == 5
    assert health.records[-1].new_items == 5 and health.records[-1].gate6 is False


def test_sixty_second_hard_minimum(cfg, clock, et) -> None:
    now = et("2026-11-02", "10:00")
    poller, fetcher, _ = _poller(cfg, clock)
    assert poller.spacing_s >= HARD_MIN_SPACING_S == 60.0
    poller.poll(now)
    assert poller.poll(now.replace(second=59)) == 0
    assert len(fetcher.calls) == 1  # no request inside 60 s
    poller.poll(et("2026-11-02", "10:01"))
    assert len(fetcher.calls) == 2


def test_repeat_poll_adds_nothing_and_raw_saved_only_on_change(cfg, clock, et) -> None:
    poller, fetcher, (raw, facts, health, _b) = _poller(cfg, clock)
    poller.poll(et("2026-11-02", "10:00"))
    assert poller.poll(et("2026-11-02", "10:01")) == 0
    assert len(facts.facts) == 5 and len(raw.saved) == 1
    assert len(health.records) == 2


def test_later_resumption_writes_a_new_fact(cfg, clock, et) -> None:
    first = halts_body()
    resumed = _swap(b"<ndaq:HaltTime>09:45:00</ndaq:HaltTime>\n<ndaq:IssueSymbol>WXYZ</ndaq:IssueSymbol>",
                    b"<ndaq:HaltTime>09:45:00</ndaq:HaltTime>\n<ndaq:IssueSymbol>WXYZ</ndaq:IssueSymbol>", first)
    resumed = resumed.replace(
        b"<ndaq:ReasonCode>M</ndaq:ReasonCode>\n<ndaq:PauseThresholdPrice></ndaq:PauseThresholdPrice>\n"
        b"<ndaq:ResumptionDate></ndaq:ResumptionDate>\n<ndaq:ResumptionQuoteTime></ndaq:ResumptionQuoteTime>\n"
        b"<ndaq:ResumptionTradeTime></ndaq:ResumptionTradeTime>",
        b"<ndaq:ReasonCode>M</ndaq:ReasonCode>\n<ndaq:PauseThresholdPrice></ndaq:PauseThresholdPrice>\n"
        b"<ndaq:ResumptionDate>11/02/2026</ndaq:ResumptionDate>\n<ndaq:ResumptionQuoteTime>09:50:00"
        b"</ndaq:ResumptionQuoteTime>\n<ndaq:ResumptionTradeTime>09:50:00</ndaq:ResumptionTradeTime>")
    assert resumed != first
    poller, fetcher, (_r, facts, _h, _b) = _poller(cfg, clock, first)
    poller.poll(et("2026-11-02", "09:46"))
    fetcher.results[HALTS_URL] = resumed
    assert poller.poll(et("2026-11-02", "09:51")) == 1
    later = facts.facts[-1]
    assert later.ticker == "WXYZ" and later.resumed_at == "2026-11-02T14:50:00Z"
    assert "|resumed|" in later.guid and later.extra["halt_key"] == halt_key("WXYZ", "11/02/2026", "09:45:00")
    assert later.first_seen_at == "2026-11-02T14:51:00Z"
    assert poller.poll(et("2026-11-02", "09:52")) == 0  # the same resumption is not written twice


def test_schema_drift_records_schema_and_no_facts(cfg, clock, et) -> None:
    bad = b'<?xml version="1.0"?><rss version="2.0"></rss>'  # no channel
    poller, _f, (raw, facts, health, board) = _poller(cfg, clock, bad)
    assert poller.poll(et("2026-11-02", "10:00")) == 0
    assert facts.facts == [] and raw.saved == []
    assert health.records[-1].result == "schema"
    assert board.sources["nasdaq_halts"].status == "unavailable"


def test_fetch_failure_marks_the_source_unavailable(cfg, clock, et) -> None:
    results = {HALTS_URL: fetch_result("nasdaq_halts", HALTS_URL, None, result="http_error", status=503)}
    poller, _f, (_r, facts, health, board) = _poller(cfg, clock, results=results)
    poller.poll(et("2026-11-02", "10:00"))
    state = board.sources["nasdaq_halts"]
    assert state.status == "unavailable" and state.gap_open is True and state.last_error == "http_error"
    assert health.records[-1].result == "http_error" and health.records[-1].http_status == 503


def test_not_modified_keeps_the_source_ok(cfg, clock, et) -> None:
    results = {HALTS_URL: fetch_result("nasdaq_halts", HALTS_URL, None, result="not_modified", status=304)}
    poller, _f, (_r, facts, _h, board) = _poller(cfg, clock, results=results)
    poller.poll(et("2026-11-02", "10:00"))
    assert board.sources["nasdaq_halts"].status == "ok" and facts.facts == []


def test_gap_before_after_a_long_silence(cfg, clock, et) -> None:
    poller, _f, (_r, _facts, health, _b) = _poller(cfg, clock)
    poller.poll(et("2026-11-02", "10:00"))
    poller.poll(et("2026-11-02", "10:01"))
    poller.poll(et("2026-11-02", "10:30"))
    assert [h.gap_before for h in health.records] == [False, False, True]


def test_dated_url() -> None:
    assert dated_url(HALTS_URL, datetime(2026, 11, 2).date()) == HALTS_URL + "&haltdate=11022026"
    assert dated_url("https://x/rss", datetime(2026, 1, 5).date()) == "https://x/rss?haltdate=01052026"


def test_halts_tick_schedule(cfg, clock, et) -> None:
    dated = dated_url(HALTS_URL, datetime(2026, 11, 2).date())
    poller, fetcher, (_r, facts, _h, _b) = _poller(cfg, clock, results={HALTS_URL: halts_body(),
                                                                        dated: halts_body()})
    halts_tick(poller, cfg, et("2026-11-02", "03:59"))  # before active hours: nothing
    assert fetcher.calls == []
    halts_tick(poller, cfg, et("2026-11-02", "19:59") + timedelta(seconds=30))
    assert fetcher.calls[-1][1] == HALTS_URL
    halts_tick(poller, cfg, et("2026-11-02", "20:00"))  # inside 60 s of the last request: waits
    assert len(fetcher.calls) == 1
    halts_tick(poller, cfg, et("2026-11-02", "20:01"))
    assert fetcher.calls[-1][1] == dated
    assert any(f.feed == FEED_DATED for f in facts.facts) is False  # every row already seen live
    for hhmm in ("20:05", "21:00", "23:59"):
        halts_tick(poller, cfg, et("2026-11-02", hhmm))
    assert len(fetcher.calls) == 2  # the dated read happens once a day
    halts_tick(poller, cfg, et("2026-11-07", "20:30"))  # Saturday: no dated read
    assert len(fetcher.calls) == 2


def test_dated_read_finds_rows_missed_live(cfg, clock, et) -> None:
    dated = dated_url(HALTS_URL, datetime(2026, 11, 2).date())
    poller, fetcher, (_r, facts, _h, _b) = _poller(
        cfg, clock, results={HALTS_URL: halts_body("tradehalts_empty_SYNTHETIC.xml"), dated: halts_body()})
    halts_tick(poller, cfg, et("2026-11-02", "19:00"))
    assert facts.facts == []
    halts_tick(poller, cfg, et("2026-11-02", "20:10"))
    assert len(facts.facts) == 5 and {f.feed for f in facts.facts} == {FEED_DATED}


def test_resumption_after_a_restart_is_still_recorded(cfg, clock, et) -> None:
    """Before the restart the halt was stored without a resumption; afterwards the row shows one.
    The halt fact itself is a duplicate (same fact_id), so a resumption fact must be written."""
    first_poller, _f, (_r, facts, _h, _b) = _poller(cfg, clock, halts_body())
    first_poller.poll(et("2026-11-02", "09:46"))
    wxyz = [f for f in facts.facts if f.ticker == "WXYZ"]
    assert len(wxyz) == 1 and wxyz[0].resumed_at is None
    resumed = halts_body().replace(
        b"<ndaq:ResumptionDate></ndaq:ResumptionDate>\n<ndaq:ResumptionQuoteTime></ndaq:ResumptionQuoteTime>\n"
        b"<ndaq:ResumptionTradeTime></ndaq:ResumptionTradeTime>\n</item>\n<item>\n<title>ABCD",
        b"<ndaq:ResumptionDate>11/02/2026</ndaq:ResumptionDate>\n<ndaq:ResumptionQuoteTime></ndaq:ResumptionQuoteTime>\n"
        b"<ndaq:ResumptionTradeTime>09:50:00</ndaq:ResumptionTradeTime>\n</item>\n<item>\n<title>ABCD", 1)
    assert resumed != halts_body()
    restarted = HaltPoller(cfg, FakeFetcher({HALTS_URL: resumed}), FakeRawSink(), facts, FakeHealthSink(),
                           FakeBoard(), clock)  # same fact store, fresh memory
    # WXYZ's resumption, plus one resumption fact each for ABCD, NEWS and ARCA: after a restart the
    # poller cannot tell whether their stored halt facts carried the resumption, so it records it
    # again (harmless for readers; stable fact_ids keep later restarts from repeating it)
    assert restarted.poll(et("2026-11-02", "09:55")) == 4
    later = [f for f in facts.facts if f.ticker == "WXYZ"][-1]
    assert later.resumed_at == "2026-11-02T14:50:00Z" and "|resumed|" in later.guid


@pytest.mark.parametrize("failure", ["timeout", "http_error", "backoff", "schema"])
def test_a_failed_dated_read_is_tried_again_the_same_evening(cfg, clock, et, failure) -> None:
    """Regression: dated_done was set before the read, so one failed `&haltdate=` request lost the
    day's reconciliation until the next day."""
    dated = dated_url(HALTS_URL, datetime(2026, 11, 2).date())
    bad = (b"<rss><nope/></rss>" if failure == "schema"
           else fetch_result("nasdaq_halts", dated, None, result=failure, status=None))
    poller, fetcher, (_r, facts, health, _b) = _poller(cfg, clock, results={dated: bad})
    halts_tick(poller, cfg, et("2026-11-02", "20:01"))
    assert len(fetcher.calls) == 1 and poller.dated_done is None
    halts_tick(poller, cfg, et("2026-11-02", "20:01") + timedelta(seconds=30))  # 60 s spacing kept
    assert len(fetcher.calls) == 1
    fetcher.results[dated] = halts_body()
    halts_tick(poller, cfg, et("2026-11-02", "20:02"))
    assert len(fetcher.calls) == 2 and poller.dated_done == datetime(2026, 11, 2).date()
    assert any(f.feed == FEED_DATED for f in facts.facts)
    halts_tick(poller, cfg, et("2026-11-02", "20:10"))
    assert len(fetcher.calls) == 2  # done for the day


def test_ready_uses_the_fetchers_send_time(cfg, clock, et) -> None:
    """Regression: the poller timed its 60 s from the tick, the fetcher from the real send time, so a
    slow tick made the fetcher sleep inside the loop every minute; and after a restart (poller time
    None) the next poll went out at once. The poller now waits for the fetcher's send time too."""
    poller, fetcher, _sinks = _poller(cfg, clock)
    sent = et("2026-11-02", "10:00") + timedelta(seconds=40)
    fetcher.last_request_at = lambda host: sent if host == "www.nasdaqtrader.com" else None
    assert poller.ready(et("2026-11-02", "10:01")) is False  # 20 s after the real send
    assert poller.poll(et("2026-11-02", "10:01")) == 0 and fetcher.calls == []
    assert poller.ready(et("2026-11-02", "10:01") + timedelta(seconds=40)) is True


def test_a_failed_fact_write_is_tried_again_on_the_next_poll(cfg, clock, et) -> None:
    """Regression: the halt key was marked seen before facts.add, so a failed write (a Windows lock
    on the day's facts file) lost that halt until a restart. The key is now marked after the write,
    and the next read is unconditional so a 304 cannot hide the row."""
    from helpers_fakes import FakeFactSink

    class FlakySink(FakeFactSink):
        fail = True

        def add(self, fact):
            if self.fail and fact.ticker == "WXYZ":
                raise OSError("locked")
            return super().add(fact)

    class Recording(FakeFetcher):
        conditional: list = []

        def fetch(self, source, url, *, conditional=True, max_bytes=None):
            self.conditional.append(conditional)
            return super().fetch(source, url, conditional=conditional, max_bytes=max_bytes)

    fetcher, facts = Recording({HALTS_URL: halts_body()}), FlakySink()
    poller = HaltPoller(cfg, fetcher, FakeRawSink(), facts, FakeHealthSink(), FakeBoard(), clock)
    with pytest.raises(OSError):
        poller.poll(et("2026-11-02", "10:00"))
    facts.fail = False
    poller.poll(et("2026-11-02", "10:01"))
    assert "WXYZ" in {f.ticker for f in facts.facts}
    assert fetcher.conditional == [True, False]
    poller.poll(et("2026-11-02", "10:02"))
    assert fetcher.conditional[-1] is True and len(facts.facts) == 5


def test_a_clock_stepped_back_does_not_stop_the_halt_polls(cfg, clock, et) -> None:
    """Regression: with the last request in the future (the wall clock stepped back), ready() stayed
    False until the clock caught up, so halts were not polled for hours."""
    poller, fetcher, _sinks = _poller(cfg, clock)
    poller.poll(et("2026-11-02", "13:00"))
    assert poller.ready(et("2026-11-02", "10:00")) is True
    poller.poll(et("2026-11-02", "10:00"))
    assert len(fetcher.calls) == 2


def test_a_dtd_in_a_utf16_halt_feed_is_refused() -> None:
    """Regression (third review): the DOCTYPE/ENTITY byte check missed a UTF-16 body."""
    body = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE rss [<!ENTITY a "x">]>'
            '<rss><channel/></rss>').encode("utf-16")
    with pytest.raises(SchemaError, match="DTD"):
        parse_halts(body)


def test_a_bad_row_is_logged_and_the_rest_are_stored(cfg, clock, et, caplog) -> None:  # noqa: ANN001
    bad = _swap(b"<ndaq:HaltTime>09:45:00</ndaq:HaltTime>", b"<ndaq:HaltTime>9:45</ndaq:HaltTime>")
    bad = _swap(b"<ndaq:Market>N</ndaq:Market>", b"<ndaq:Market>NYSE</ndaq:Market>", bad)
    poller, _f, (_raw, facts, health, board) = _poller(cfg, clock, bad)
    with caplog.at_level("WARNING", logger="scout.halts"):
        assert poller.poll(et("2026-11-02", "10:00")) == 4
    assert health.records[-1].result == "ok" and health.records[-1].error.startswith("skipped 1 bad row(s)")
    assert health.records[-1].error.startswith(reader_blocks._SKIPPED)  # noqa: SLF001 - entry_blocks reads it
    assert board.sources["nasdaq_halts"].status == "ok"
    assert any("halts row skipped" in r.getMessage() for r in caplog.records)
    assert "WXYZ" not in {f.ticker for f in facts.facts}  # the bad row; the NYSE market alone is fine
