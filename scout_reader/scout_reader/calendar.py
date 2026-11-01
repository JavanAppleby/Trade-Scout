"""US market calendar: NYSE holidays, early closes, session times, trading-day arithmetic.

PROVENANCE (SCOUT_SPEC 3, DESIGN D1 and 3.4): the pure parts of Trade Guardian's
`trade_guardian/clock.py`, copied from commit 72dccdc2e4f865ba9c50848fa49e6717a80712ed
(file sha256 7c3cf51168d48c4bc1a5bda13d947aae60196dde53365527f448b723e4f9206d).
Changes from the source: ET comes from `scout_reader._et` (stdlib, no `zoneinfo`/`tzdata`), so
`et_datetime` returns a fixed-offset EST/EDT datetime for the same instant Guardian's returns; the
clocks, `Session`, sessions and parsing helpers are not copied here (Scout's `scout.clock` and
`scout.calendar` hold them); `prior_close_before` and `next_open_after` are added for the gates.
Scout never installs Trade Guardian (SS 2.1). tests/test_calendar.py compares this copy with
Guardian's answers for every day of 2026-2027 (tests/fixtures/guardian_calendar_2026_2027.json).
When Guardian's tables change, copy them here and regenerate that fixture with
tools/make_calendar_fixture.py.

Sessions: PRE 04:00-09:30, CORE 09:30-16:00, POST 16:00-20:00 ET. On early-close days CORE ends
13:00 and POST runs 13:00-17:00.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from scout_reader._et import et_to_utc, utc_to_et

GUARDIAN_COMMIT = "72dccdc2e4f865ba9c50848fa49e6717a80712ed"
GUARDIAN_CLOCK_SHA256 = "7c3cf51168d48c4bc1a5bda13d947aae60196dde53365527f448b723e4f9206d"

PRE_OPEN = time(4, 0)
CORE_OPEN = time(9, 30)
CORE_CLOSE = time(16, 0)
POST_CLOSE = time(20, 0)
EARLY_CORE_CLOSE = time(13, 0)
EARLY_POST_CLOSE = time(17, 0)
DAY_ROLLOVER = time(20, 0)  # trading-day boundary (Guardian's rule)
NIGHT_OPEN = time(20, 0)

# NYSE full-day closures. Verify against nyse.com when extending; years outside
# CALENDAR_YEARS fall back to "every weekday trades" (calendar_covers() lets callers warn).
HOLIDAYS: frozenset[date] = frozenset({
    # 2026
    date(2026, 1, 1),  # New Year's Day
    date(2026, 1, 19),  # Martin Luther King Jr. Day
    date(2026, 2, 16),  # Washington's Birthday
    date(2026, 4, 3),  # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),  # Independence Day (observed; July 4 is a Saturday)
    date(2026, 9, 7),  # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
    # 2027
    date(2027, 1, 1),  # New Year's Day
    date(2027, 1, 18),  # Martin Luther King Jr. Day
    date(2027, 2, 15),  # Washington's Birthday
    date(2027, 3, 26),  # Good Friday
    date(2027, 5, 31),  # Memorial Day
    date(2027, 6, 18),  # Juneteenth (observed; June 19 is a Saturday)
    date(2027, 7, 5),  # Independence Day (observed; July 4 is a Sunday)
    date(2027, 9, 6),  # Labor Day
    date(2027, 11, 25),  # Thanksgiving
    date(2027, 12, 24),  # Christmas (observed; Dec 25 is a Saturday)
    # No 2027-12-31 closure: NYSE does not observe a Saturday New Year on the Friday before.
})

# 13:00 ET early closes. No July early close in either year.
EARLY_CLOSES: frozenset[date] = frozenset({
    date(2026, 11, 27),  # day after Thanksgiving
    date(2026, 12, 24),  # Christmas Eve
    date(2027, 11, 26),  # day after Thanksgiving
})

CALENDAR_YEARS: frozenset[int] = frozenset({2026, 2027})

_SEARCH_DAYS = 14  # more than any run of consecutive non-trading days


@dataclass(frozen=True)
class SessionTimes:
    """Session boundaries of one trading day, as aware ET datetimes."""

    day: date
    pre_open: datetime
    core_open: datetime
    core_close: datetime
    post_close: datetime
    early_close: bool


def calendar_covers(day: date) -> bool:
    """True when the holiday table knows `day`'s year."""
    return day.year in CALENDAR_YEARS


def is_holiday(day: date) -> bool:
    return day in HOLIDAYS


def is_early_close(day: date) -> bool:
    return day in EARLY_CLOSES


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in HOLIDAYS


def next_trading_day(day: date) -> date:
    """First trading day strictly after `day`."""
    for offset in range(1, _SEARCH_DAYS + 1):
        candidate = day + timedelta(days=offset)
        if is_trading_day(candidate):
            return candidate
    raise RuntimeError(f"no trading day within {_SEARCH_DAYS} days after {day}")


def previous_trading_day(day: date) -> date:
    """Last trading day strictly before `day`."""
    for offset in range(1, _SEARCH_DAYS + 1):
        candidate = day - timedelta(days=offset)
        if is_trading_day(candidate):
            return candidate
    raise RuntimeError(f"no trading day within {_SEARCH_DAYS} days before {day}")


def et_datetime(day: date, at: time) -> datetime:
    """`day` at wall-clock `at` in New York (correct offset on either side of DST), aware ET."""
    naive = datetime(day.year, day.month, day.day, at.hour, at.minute, at.second)
    return utc_to_et(et_to_utc(naive))


def session_times(day: date) -> SessionTimes | None:
    """Boundaries for `day`, or None when the market is closed all day."""
    if not is_trading_day(day):
        return None
    early = is_early_close(day)
    return SessionTimes(
        day=day,
        pre_open=et_datetime(day, PRE_OPEN),
        core_open=et_datetime(day, CORE_OPEN),
        core_close=et_datetime(day, EARLY_CORE_CLOSE if early else CORE_CLOSE),
        post_close=et_datetime(day, EARLY_POST_CLOSE if early else POST_CLOSE),
        early_close=early,
    )


def pre_open(day: date) -> datetime | None:
    st = session_times(day)
    return st.pre_open if st else None


def core_open(day: date) -> datetime | None:
    st = session_times(day)
    return st.core_open if st else None


def core_close(day: date) -> datetime | None:
    st = session_times(day)
    return st.core_close if st else None


def post_close(day: date) -> datetime | None:
    st = session_times(day)
    return st.post_close if st else None


# --------------------------------------------------------------------------- Scout additions


def prior_close_before(as_of: datetime) -> datetime:
    """UTC close (16:00 ET, 13:00 on early closes) of the last trading day whose close is at or
    before `as_of` (aware)."""
    day = utc_to_et(as_of).date()
    for offset in range(0, _SEARCH_DAYS + 1):
        close = core_close(day - timedelta(days=offset))
        if close is not None and close <= as_of:
            return close.astimezone(timezone.utc)
    raise RuntimeError(f"no close within {_SEARCH_DAYS} days before {as_of}")  # pragma: no cover


def next_open_after(as_of: datetime) -> datetime | None:
    """UTC 09:30 ET open of the first trading day whose open is strictly after `as_of` (aware);
    None when the calendar does not cover that day (gate 5 then answers unknown)."""
    day = utc_to_et(as_of).date()
    for offset in range(0, _SEARCH_DAYS + 1):
        candidate = day + timedelta(days=offset)
        if not calendar_covers(candidate):
            return None
        opening = core_open(candidate)
        if opening is not None and opening > as_of:
            return opening.astimezone(timezone.utc)
    return None  # pragma: no cover


__all__ = [
    "CALENDAR_YEARS", "CORE_CLOSE", "CORE_OPEN", "DAY_ROLLOVER", "EARLY_CLOSES", "EARLY_CORE_CLOSE",
    "EARLY_POST_CLOSE", "GUARDIAN_CLOCK_SHA256", "GUARDIAN_COMMIT", "HOLIDAYS", "NIGHT_OPEN", "POST_CLOSE",
    "PRE_OPEN", "SessionTimes", "calendar_covers", "core_close", "core_open", "et_datetime", "is_early_close",
    "is_holiday", "is_trading_day", "next_open_after", "next_trading_day", "post_close", "pre_open",
    "previous_trading_day", "prior_close_before", "session_times",
]
