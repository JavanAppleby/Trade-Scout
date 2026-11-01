"""Cboe VIX history CSV (SCOUT_SPEC 4.1 #10, 7.6, 14; DESIGN 2.3)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from helpers_fakes import FakeBoard, FakeFetcher, FakeHealthSink, FakeRawSink, fetch_result
from helpers_macro import VIX_URL, macro_body

from scout.cboe_vix import VixReader, latest, parse_vix_csv
from scout.macro import SchemaError

UTC = timezone.utc
BODY = macro_body("VIX_History_head_SYNTHETIC.csv")


def test_parses_the_head() -> None:
    rows = parse_vix_csv(BODY)
    assert len(rows) == 7 and rows[0].day == date(1990, 1, 2)
    last = latest(rows)
    assert (last.day, last.close) == (date(2026, 10, 30), 17.12)


def test_bom_crlf_iso_dates_and_order_are_tolerated() -> None:
    body = b"\xef\xbb\xbf" + b"DATE,OPEN,HIGH,LOW,CLOSE\r\n2026-10-30,1,2,0.5,1.5\r\n2026-10-29,1,2,0.5,1.4\r\n\r\n"
    assert latest(parse_vix_csv(body)).close == 1.5


@pytest.mark.parametrize("body", [
    b"", b"<html>", b"Date,Open,High,Low,Close\n10/30/2026,1,2,0.5,1.5\n",  # header case drift
    b"DATE,OPEN,HIGH,LOW,CLOSE,VOLUME\n10/30/2026,1,2,0.5,1.5,9\n",
    b"DATE,OPEN,HIGH,LOW\n10/30/2026,1,2,0.5\n",
    b"DATE,OPEN,HIGH,LOW,CLOSE\n",  # no rows
    b"DATE,OPEN,HIGH,LOW,CLOSE\n10/30/2026,1,2,0.5\n",
    b"DATE,OPEN,HIGH,LOW,CLOSE\n10/30/2026,1,2,0.5,n/a\n",
    b"DATE,OPEN,HIGH,LOW,CLOSE\n10/30/2026,1,2,0.5,-3\n",
    b"DATE,OPEN,HIGH,LOW,CLOSE\n30/10/2026,1,2,0.5,1.5\n",
    b"DATE,OPEN,HIGH,LOW,CLOSE\n10/30/2026,1,2,0.5,nan\n",
])
def test_drift_refuses_the_source(body: bytes) -> None:
    with pytest.raises(SchemaError):
        parse_vix_csv(body)


def _reader(cfg, clock, result):
    health, board = FakeHealthSink(), FakeBoard()
    return VixReader(cfg, FakeFetcher({VIX_URL: result}), FakeRawSink(), health, board, clock), health, board


def test_reader_ok_on_the_next_trading_day(cfg, clock) -> None:
    reader, health, board = _reader(cfg, clock, BODY)
    rec = reader.read(datetime(2026, 11, 2, 14, tzinfo=UTC))  # Monday: Friday's close is current
    assert rec.source == "cboe_vix" and rec.status == "ok" and rec.vix == {"date": "2026-10-30", "close": 17.12}
    assert board.sources["cboe_vix"].status == "ok" and health.records[-1].items == 7


def test_reader_old_close_is_stale(cfg, clock) -> None:
    rec = _reader(cfg, clock, BODY)[0].read(datetime(2026, 11, 4, 22, tzinfo=UTC))
    assert rec.status == "stale" and rec.vix["close"] == 17.12
    assert rec.reasons == ("vix_close_older_than_previous_trading_day",)


def test_reader_failures(cfg, clock) -> None:
    rec = _reader(cfg, clock, b"DATE,CLOSE\n")[0].read(datetime(2026, 11, 2, 14, tzinfo=UTC))
    assert rec.status == "unavailable" and rec.vix is None and "schema" in rec.reasons
    reader, health, board = _reader(cfg, clock, fetch_result("cboe_vix", VIX_URL, None, result="network", status=None))
    rec = reader.read(datetime(2026, 11, 2, 14, tzinfo=UTC))
    assert rec.status == "unavailable" and board.sources["cboe_vix"].status == "unavailable"
