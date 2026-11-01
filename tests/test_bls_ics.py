"""BLS release schedule .ics: lenient parser, strict on drift (SCOUT_SPEC 4.1 #8, 14; DESIGN 2.3)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from helpers_fakes import FakeBoard, FakeFetcher, FakeHealthSink, FakeRawSink, fetch_result
from helpers_macro import BLS_URL, macro_body

from scout.bls_ics import BlsReader, bls_events, parse_bls_ics, parse_ics
from scout.macro import SchemaError

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)


def _ics(*lines: str, crlf: bool = True) -> bytes:
    body = ["BEGIN:VCALENDAR", "VERSION:2.0", *lines, "END:VCALENDAR", ""]
    return ("\r\n" if crlf else "\n").join(body).encode()


def _event(*props: str) -> tuple[str, ...]:
    return ("BEGIN:VEVENT", *props, "END:VEVENT")


def test_fixture_without_dtstamp_parses() -> None:
    body = macro_body("bls_SYNTHETIC.ics")
    assert b"\nDTSTAMP" not in body
    events = parse_bls_ics(body)
    assert len(events) == 5
    by = {e.uid: e for e in events}
    assert by["bls-cpi-2026-10"].start == datetime(2026, 11, 12, 13, 30, tzinfo=UTC)  # 08:30 EST
    assert by["bls-cpi-2026-11"].start == datetime(2026, 12, 10, 13, 30, tzinfo=UTC)  # America/New_York
    assert by["bls-empsit-2026-10"].summary == "Employment Situation for October 2026"  # folded line joined
    assert all(e.tz == "et" for e in events)


def test_bls_events_keep_cpi_and_nfp_only() -> None:
    events, other = bls_events(parse_bls_ics(macro_body("bls_SYNTHETIC.ics")))
    assert [(e.kind, e.event_at.date().isoformat()) for e in events] == [
        ("nfp", "2026-11-06"), ("cpi", "2026-11-12"), ("nfp", "2026-12-04"), ("cpi", "2026-12-10")]
    assert other == 1 and {e.source for e in events} == {"bls"}


def test_summer_time_is_edt() -> None:
    body = _ics(*_event("DTSTART;TZID=US-Eastern:20260710T083000", "SUMMARY:Consumer Price Index for June 2026"))
    assert parse_ics(body)[0].start == datetime(2026, 7, 10, 12, 30, tzinfo=UTC)


def test_lf_endings_bom_utc_and_floating_times() -> None:
    body = b"\xef\xbb\xbf" + _ics(*_event("DTSTART:20261112T133000Z", "SUMMARY:A"),
                                  *_event("DTSTART:20261112T083000", "SUMMARY:B"),
                                  *_event("DTSTART;VALUE=DATE:20261126", "SUMMARY:Holiday"), crlf=False)
    a, b, c = parse_ics(body)
    assert a.start == b.start == datetime(2026, 11, 12, 13, 30, tzinfo=UTC)
    assert (a.tz, b.tz) == ("utc", "floating-et")
    assert c.all_day is True
    events, other = bls_events([a, b, c])
    assert events == [] and other == 3


def test_unknown_properties_and_components_are_ignored() -> None:
    body = _ics("X-WR-CALNAME:BLS", "BEGIN:VTIMEZONE", "TZID:US-Eastern", "END:VTIMEZONE",
                *_event("DTSTART;TZID=US-Eastern:20261106T083000", "X-FOO;BAR=1:baz", "DESCRIPTION:a\\, b",
                        "SUMMARY:Employment Situation for October 2026"))
    assert len(parse_ics(body)) == 1


@pytest.mark.parametrize("body", [
    b"", b"not a calendar", b"BEGIN:VEVENT\r\nEND:VEVENT\r\n",
    _ics(*_event("SUMMARY:no start")),
    _ics(*_event("DTSTART;TZID=Europe/London:20261106T083000", "SUMMARY:x")),
    _ics(*_event("DTSTART;TZID=US-Eastern:2026-11-06 08:30", "SUMMARY:x")),
    _ics(*_event("DTSTART;TZID=US-Eastern:20261306T083000", "SUMMARY:x")),
    _ics("BEGIN:VEVENT", "DTSTART:20261106T133000Z"),  # never closed
    _ics("BEGIN:VEVENT", "BEGIN:VEVENT", "DTSTART:20261106T133000Z", "END:VEVENT", "END:VEVENT"),
    _ics(*_event("DTSTART:20261106T133000Z", "this line has no colon")),
    b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:20261106T133000Z\r\nEND:VEVENT\r\n",  # truncated
])
def test_drift_refuses_the_source(body: bytes) -> None:
    with pytest.raises(SchemaError):
        parse_ics(body)


def _reader(cfg, clock, result):
    fetcher = FakeFetcher({BLS_URL: result})
    health, board = FakeHealthSink(), FakeBoard()
    return BlsReader(cfg, fetcher, FakeRawSink(), health, board, clock), fetcher, health, board


def test_reader_ok(cfg, clock) -> None:
    reader, fetcher, health, board = _reader(cfg, clock, macro_body("bls_SYNTHETIC.ics"))
    rec = reader.read(NOW)
    assert rec.source == "bls" and rec.status == "ok" and len(rec.events) == 4
    assert rec.events[0] == {"kind": "nfp", "event_at": "2026-11-06T13:30:00Z", "prior": None, "street": None,
                             "nowflation_forecast": None, "cleveland": None, "kalshi": None,
                             "extra": {"uid": "bls-empsit-2026-10", "title": "Employment Situation for October 2026"}}
    assert "other_events_left_out:1" in rec.reasons
    assert fetcher.calls == [("bls", BLS_URL)]
    assert board.sources["bls"].status == "ok" and health.records[-1].result == "ok"


def test_reader_schema_and_fetch_failures(cfg, clock) -> None:
    rec = _reader(cfg, clock, b"<html>moved</html>")[0].read(NOW)
    assert rec.status == "unavailable" and rec.events == () and "schema" in rec.reasons
    reader, _f, health, board = _reader(cfg, clock, fetch_result("bls", BLS_URL, None, result="timeout", status=None))
    rec = reader.read(NOW)
    assert rec.status == "unavailable" and rec.reasons == ("fetch:timeout",)
    assert board.sources["bls"].status == "unavailable" and health.records[-1].result == "timeout"


def test_reader_no_release_events_is_stale(cfg, clock) -> None:
    body = _ics(*_event("DTSTART;TZID=US-Eastern:20261113T083000", "SUMMARY:Producer Price Index"))
    rec = _reader(cfg, clock, body)[0].read(NOW)
    assert rec.status == "stale" and "no_cpi_or_nfp_events" in rec.reasons


def test_reader_with_only_past_events_is_stale(cfg, clock) -> None:
    """Regression: a file holding only last year's schedule was `ok`, so the reader's macro
    fallback could answer false with no CPI or NFP known ahead (SS 7.3: then unknown)."""
    reader = _reader(cfg, clock, macro_body("bls_SYNTHETIC.ics"))[0]
    rec = reader.read(datetime(2027, 1, 5, 14, 0, tzinfo=UTC))  # every event in the file is past
    assert rec.status == "stale" and "no_cpi_and_nfp_after_now" in rec.reasons
    only_cpi = _ics(*_event("DTSTART;TZID=US-Eastern:20261112T083000", "SUMMARY:Consumer Price Index for October"))
    rec = _reader(cfg, clock, only_cpi)[0].read(NOW)
    assert rec.status == "stale"  # an NFP ahead is needed too
