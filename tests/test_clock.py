"""Clocks and time helpers (DESIGN 9)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from scout.clock import (
    Clock, ManualClock, SystemClock, et_date, et_datetime, fmt_et_bne, iso, parse_hhmm, parse_iso, to_brisbane,
)

from conftest import START


def test_manual_clock_semantics() -> None:
    c = ManualClock(START)
    assert isinstance(c, Clock) and c.now() == START and c.monotonic() == 0
    c.sleep(30)
    assert c.now() == START + timedelta(seconds=30) and c.monotonic() == 30
    c.sleep(-5)  # a negative sleep does nothing
    assert c.monotonic() == 30
    with pytest.raises(ValueError, match="backwards"):
        c.advance(-1)
    with pytest.raises(ValueError):
        ManualClock(datetime(2026, 11, 2))
    c.set(datetime(2026, 11, 3, 1, tzinfo=timezone(timedelta(hours=10))))
    assert c.now() == datetime(2026, 11, 2, 15, tzinfo=timezone.utc)


def test_system_clock_is_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0)


def test_iso_and_parse() -> None:
    assert iso(START) == "2026-11-02T14:00:00Z"
    assert iso(START + timedelta(microseconds=500)) == "2026-11-02T14:00:00.000500Z"
    assert parse_iso("2026-11-02T14:00:00Z") == START
    assert parse_iso("2026-11-03T00:00:00+10:00") == START
    assert parse_iso("2026-11-02T14:00:00") is None  # naive refused
    assert parse_iso("garbage") is None and parse_iso(None) is None
    with pytest.raises(ValueError):
        iso(datetime(2026, 11, 2))


def test_et_helpers_and_brisbane() -> None:
    assert et_date(datetime(2026, 11, 3, 4, 59, tzinfo=timezone.utc)) == date(2026, 11, 2)  # 23:59 ET
    assert et_datetime(date(2026, 11, 2), time(9, 30)) == datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)
    assert et_datetime(date(2026, 7, 1), time(9, 30)) == datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)
    assert to_brisbane(START).hour == 0 and to_brisbane(START).utcoffset() == timedelta(hours=10)
    assert fmt_et_bne(START) == "09:00 ET / 00:00 AEST"


@pytest.mark.parametrize("text", ["8:20", "24:00", "12:60", "", "ab:cd", None, "12:00:00"])
def test_parse_hhmm_rejects(text) -> None:  # noqa: ANN001
    with pytest.raises(ValueError):
        parse_hhmm(text)


def test_parse_hhmm() -> None:
    assert parse_hhmm(" 08:20 ") == time(8, 20)
