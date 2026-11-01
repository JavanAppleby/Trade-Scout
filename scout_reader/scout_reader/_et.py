"""UTC <-> US Eastern time by the US daylight-saving rule, stdlib only (DESIGN D2).

`zoneinfo` needs the `tzdata` package on Windows, which is not stdlib, so the reader carries the
rule itself: Eastern Daylight Time (UTC-4) from the second Sunday of March at 02:00 local to the
first Sunday of November at 02:00 local; Eastern Standard Time (UTC-5) otherwise. The rule has held
since 2007. A wall time in the spring gap or the autumn repeat resolves as `zoneinfo` does with
`fold=0` (the offset in force before the change). tests/test_et.py compares this module with
`zoneinfo` for every hour of 2026-2027.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

EST = timezone(timedelta(hours=-5), "EST")
EDT = timezone(timedelta(hours=-4), "EDT")

__all__ = ["EDT", "EST", "dst_bounds_utc", "et_offset", "et_to_utc", "utc_to_et"]


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
    return first_sunday + timedelta(weeks=n - 1)


def dst_bounds_utc(year: int) -> tuple[datetime, datetime]:
    """[start, end) of daylight time in `year`, as aware UTC datetimes."""
    start = _nth_sunday(year, 3, 2)  # 02:00 EST = 07:00 UTC
    end = _nth_sunday(year, 11, 1)  # 02:00 EDT = 06:00 UTC
    return (datetime(start.year, start.month, start.day, 7, tzinfo=timezone.utc),
            datetime(end.year, end.month, end.day, 6, tzinfo=timezone.utc))


def _require_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError("naive datetime: pass a timezone-aware value (UTC preferred)")
    return dt


def et_offset(dt: datetime) -> timezone:
    """The Eastern zone (EST or EDT) in force at the aware instant `dt`."""
    utc = _require_aware(dt).astimezone(timezone.utc)
    start, end = dst_bounds_utc(utc.year)
    return EDT if start <= utc < end else EST


def utc_to_et(dt: datetime) -> datetime:
    """The aware instant `dt` as an Eastern wall time (fixed-offset tz EST or EDT)."""
    return _require_aware(dt).astimezone(et_offset(dt))


def et_to_utc(local: datetime) -> datetime:
    """A naive Eastern wall time -> aware UTC (fold=0 semantics in the DST gap and repeat).
    An aware value is simply converted."""
    if local.tzinfo is not None and local.tzinfo.utcoffset(local) is not None:
        return local.astimezone(timezone.utc)
    year = local.year
    start, end = _nth_sunday(year, 3, 2), _nth_sunday(year, 11, 1)
    # Wall times 02:00-02:59 on the spring day do not exist: fold=0 keeps EST (the offset before).
    # Wall times 01:00-01:59 on the autumn day happen twice: fold=0 takes the first (EDT).
    dst_from = datetime(start.year, start.month, start.day, 3, 0)
    dst_to = datetime(end.year, end.month, end.day, 2, 0)
    naive = local.replace(tzinfo=None)
    tz = EDT if dst_from <= naive < dst_to else EST
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)
