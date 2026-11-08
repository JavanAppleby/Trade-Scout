"""Scout's market calendar: the Guardian copy plus sessions (SCOUT_SPEC 3, DESIGN D1 and 2.1).

The tables and trading-day arithmetic are `scout_reader.calendar` (copied from Trade Guardian's
`clock.py`, provenance in that module), re-exported here. Guardian's `clock.py` imports
`trade_guardian.models.Session`; Scout cannot install Trade Guardian (SS 2.1), so the session
functions below are copied from the same file (commit 72dccdc2e4f865ba9c50848fa49e6717a80712ed,
sha256 7c3cf51168d48c4bc1a5bda13d947aae60196dde53365527f448b723e4f9206d) over a local `Session`
enum carrying Guardian's names. tests/test_calendar.py compares every day of 2026-2027.

Sessions: PRE 04:00-09:30, CORE 09:30-16:00, POST 16:00-20:00 ET, NIGHT 20:00-04:00 ET into a
trading day (Sunday-Thursday nights), else CLOSED. A trading day rolls over at 20:00 ET.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from typing import TYPE_CHECKING

from scout_reader._et import utc_to_et
from scout_reader.calendar import *  # noqa: F403 - the re-export is this module's purpose
from scout_reader.calendar import (
    DAY_ROLLOVER, NIGHT_OPEN, PRE_OPEN, et_datetime, is_trading_day, next_trading_day, session_times,
)
from scout_reader.calendar import __all__ as _reader_all

if TYPE_CHECKING:  # pragma: no cover
    from scout.config import ScoutConfig


class Session(StrEnum):
    """US equity session, Guardian's names (trade_guardian.models.Session)."""

    PRE = "PRE"  # 04:00-09:30 ET
    CORE = "CORE"  # 09:30-16:00 ET (13:00 on early-close days)
    POST = "POST"  # 16:00-20:00 ET (13:00-17:00 on early-close days)
    NIGHT = "NIGHT"  # 20:00-04:00 ET into a trading day
    CLOSED = "CLOSED"


def _to_et(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError("naive datetime: pass a timezone-aware value (UTC preferred)")
    return utc_to_et(dt)


def night_bounds(dt: datetime) -> tuple[datetime, datetime] | None:
    """(start, end) in UTC of the overnight session containing `dt`, or None."""
    et = _to_et(dt)
    if et.time() >= NIGHT_OPEN:
        evening = et.date()
    elif et.time() < PRE_OPEN:
        evening = et.date() - timedelta(days=1)
    else:
        return None
    into = evening + timedelta(days=1)
    if not is_trading_day(into):
        return None
    return (et_datetime(evening, NIGHT_OPEN).astimezone(timezone.utc),
            et_datetime(into, PRE_OPEN).astimezone(timezone.utc))


def session_at(dt: datetime) -> Session:
    """The session live at `dt` (aware). Starts are inclusive, ends exclusive."""
    et = _to_et(dt)
    st = session_times(et.date())
    if st is not None and st.pre_open <= et < st.post_close:
        if et < st.core_open:
            return Session.PRE
        if et < st.core_close:
            return Session.CORE
        return Session.POST
    return Session.NIGHT if night_bounds(dt) is not None else Session.CLOSED


def trading_day_of(dt: datetime) -> date:
    """The trading day `dt` belongs to (rolls over at 20:00 ET, skips closed days)."""
    et = _to_et(dt)
    day = et.date()
    if et.time() >= DAY_ROLLOVER or not is_trading_day(day):
        return next_trading_day(day)
    return day


def in_active_hours(dt: datetime, cfg: ScoutConfig) -> bool:
    """True when `dt`'s ET wall time is inside `active_hours_et` [start, end) on a trading day (owner,
    2026-11-09: weekends and market holidays run at idle spacing, as overnight does)."""
    start, end = cfg.active_hours_et
    et = _to_et(dt)
    t: time = et.time()
    return is_trading_day(et.date()) and start <= t < end


__all__ = [*_reader_all, "Session", "in_active_hours", "night_bounds", "session_at", "trading_day_of"]
