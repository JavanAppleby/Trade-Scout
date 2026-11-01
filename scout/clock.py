"""Clocks and time helpers (DESIGN 2.1, 9).

Every module takes a clock; nothing calls `datetime.now()` except `SystemClock`. Stored times are
UTC ISO-8601 with `Z` (`iso`). ET comes only from `scout_reader._et`; Brisbane is fixed UTC+10 and
appears in reports only. `ManualClock` keeps Trade Guardian's semantics: a clock never runs
backwards through `advance`, and `sleep` advances it so code that sleeps runs instantly in tests.
"""

from __future__ import annotations

import time as _time
from datetime import date, datetime, time, timedelta, timezone
from typing import Protocol, runtime_checkable

from scout_reader._et import et_to_utc, utc_to_et
from scout_reader.records import parse_time

BRISBANE = timezone(timedelta(hours=10), "AEST")

__all__ = [
    "BRISBANE", "Clock", "ManualClock", "SystemClock", "et_date", "et_datetime", "fmt_et_bne", "iso",
    "parse_hhmm", "parse_iso", "to_brisbane", "to_et",
]


@runtime_checkable
class Clock(Protocol):
    """Source of the current time. Inject a ManualClock in tests."""

    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""
        ...


class SystemClock:
    """The real clock (the only caller of datetime.now)."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            _time.sleep(seconds)

    def monotonic(self) -> float:
        return _time.monotonic()


def _require_aware(dt: datetime) -> datetime:
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError("naive datetime: pass a timezone-aware value (UTC preferred)")
    return dt


class ManualClock:
    """Test clock. `sleep` advances it; `monotonic()` is seconds since construction."""

    def __init__(self, start: datetime) -> None:
        self._now = _require_aware(start).astimezone(timezone.utc)
        self._origin = self._now

    def now(self) -> datetime:
        return self._now

    def set(self, when: datetime) -> None:
        self._now = _require_aware(when).astimezone(timezone.utc)

    def advance(self, seconds: float) -> datetime:
        if seconds < 0:
            raise ValueError("a clock never runs backwards")
        self._now += timedelta(seconds=seconds)
        return self._now

    def sleep(self, seconds: float) -> None:
        self.advance(max(0.0, seconds))

    def monotonic(self) -> float:
        return (self._now - self._origin).total_seconds()


# --------------------------------------------------------------------------- formatting


def iso(dt: datetime) -> str:
    """"YYYY-MM-DDTHH:MM:SSZ" (microseconds kept only when present)."""
    utc = _require_aware(dt).astimezone(timezone.utc)
    if utc.microsecond:
        return utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: object) -> datetime | None:
    """Aware UTC from a stored ISO string ("...Z" or with an offset); None when unreadable or naive."""
    return parse_time(value)


def to_et(dt: datetime) -> datetime:
    return utc_to_et(_require_aware(dt))


def et_date(dt: datetime) -> date:
    """The ET calendar date of an aware instant (daily files are named by it, DESIGN 9)."""
    return to_et(dt).date()


def et_datetime(day: date, t: time) -> datetime:
    """`day` at ET wall time `t`, as aware UTC."""
    return et_to_utc(datetime(day.year, day.month, day.day, t.hour, t.minute, t.second))


def to_brisbane(dt: datetime) -> datetime:
    return _require_aware(dt).astimezone(BRISBANE)


def parse_hhmm(text: object) -> time:
    """Parse an "HH:MM" 24-hour wall time, e.g. "08:20"."""
    if not isinstance(text, str):
        raise ValueError(f"expected an 'HH:MM' string, got {text!r}")
    parts = text.strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() and len(p) == 2 for p in parts):
        raise ValueError(f"expected 'HH:MM', got {text!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        raise ValueError(f"not a valid time of day: {text!r}")
    return time(hour, minute)


def fmt_et_bne(dt: datetime) -> str:
    """Report format: "HH:MM ET / HH:MM AEST"."""
    return f"{to_et(dt):%H:%M} ET / {to_brisbane(dt):%H:%M} AEST"
