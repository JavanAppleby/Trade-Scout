"""The announced-date parser for gate 5 source 1 (SCOUT_SPEC 7.2 item 1, DESIGN 3.6). Stdlib only.

`announced_date(title, published_at)` reads the date an `earnings_date` wire title names, such as
"XYZ to Report Third Quarter 2026 Results on November 12, 2026" or "ABC to Announce Q3 Results Nov. 12".
It returns a `date`, or None when the title has no date it can read or the reading is ambiguous.
None is never "no event": gate 5 turns an unparsed matching title into `unknown`.

Rules (deterministic, no guessing):
- Month names, full or abbreviated ("Nov", "Nov.", "Sept."), then a day (an "st/nd/rd/th" ending is
  allowed), then an optional year ("November 12, 2026", "Nov. 12 2026"). "may" must be written
  "May" (the verb is never a month). Numeric dates ("11/12/2026") are not read: their order is
  ambiguous.
- A date right after "ended", "ending", "end of", "as of" or "through" is a period end, not the
  event, and is skipped ("for the quarter ended September 30, 2026").
- A date right after "rescheduled from", "moved from", "postponed from" or "changed from" is the old
  date, not the event, and is skipped.
- A range ("Nov. 12-13", "November 12 to 13") or two different remaining dates is ambiguous, whether
  or not one is written after "on" ("Closed Its Acquisition on October 30, to Report ... November 3"
  is ambiguous, not October 30).
- A weekday written with the date ("Thursday, November 12, 2026") must match it, else ambiguous.
- Without a year the year is taken from `published_at` (its ET date): the one candidate among the
  previous, same and next year that falls within [published - 30 days, published + 330 days].
  None or two candidates is ambiguous.
- An impossible date (February 30) is ambiguous.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from scout_reader._et import utc_to_et

__all__ = ["MONTHS", "announced_date", "dates_in"]

MONTHS: dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
    "sept": 9, "sep": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
WEEKDAYS: dict[str, int] = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2, "thursday": 3,
    "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
_WEEKDAY_ALT = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_DATE = re.compile(
    rf"(?:\b(?P<wd>{_WEEKDAY_ALT})\.?,?\s+)?"
    rf"\b(?P<month>{_MONTH_ALT})\b\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?\b"
    r"(?P<range>\s*(?:-|–|—|to|through|and|&)\s*\d{1,2}(?:st|nd|rd|th)?\b)?"
    r"(?:,?\s+(?P<year>(?:19|20)\d{2})\b)?",
    re.IGNORECASE,
)
_PERIOD_END = re.compile(r"(?:\bended|\bending|\bend\s+of|\bas\s+of|\bthrough|\bthru)\s*$", re.IGNORECASE)
_PREVIOUS = re.compile(r"\b(?:rescheduled|moved|postponed|changed)\s+from\s*$", re.IGNORECASE)
_ON = re.compile(r"\bon\s*$", re.IGNORECASE)
_LOOK_BACK = timedelta(days=30)
_LOOK_AHEAD = timedelta(days=330)

_AMBIGUOUS = object()


def _published_day(published_at: datetime | date | None) -> date | None:
    if isinstance(published_at, datetime):
        if published_at.tzinfo is None or published_at.utcoffset() is None:
            return None
        return utc_to_et(published_at).date()
    if isinstance(published_at, date):
        return published_at
    return None


def _resolve_year(month: int, day: int, published: date | None) -> date | object:
    if published is None:
        return _AMBIGUOUS
    found: list[date] = []
    for year in (published.year - 1, published.year, published.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if published - _LOOK_BACK <= candidate <= published + _LOOK_AHEAD:
            found.append(candidate)
    return found[0] if len(found) == 1 else _AMBIGUOUS


def dates_in(title: str, published_at: datetime | date | None = None) -> list[tuple[str, date | None]]:
    """Every date mention in `title` as (role, date): role "on", "period_end", "previous" or "plain"; the date
    is None when that mention is ambiguous (a range, a bad weekday, an impossible day, no year)."""
    out: list[tuple[str, date | None]] = []
    published = _published_day(published_at)
    for m in _DATE.finditer(title):
        month_text = m.group("month")
        if month_text.lower() == "may" and month_text != "May":
            continue  # the verb "may"
        before = title[:m.start()]
        if _PERIOD_END.search(before):
            role = "period_end"
        elif _PREVIOUS.search(before):
            role = "previous"
        else:
            role = "on" if _ON.search(before) else "plain"
        month, day = MONTHS[month_text.lower()], int(m.group("day"))
        value: date | None
        if m.group("range"):
            value = None
        elif m.group("year"):
            try:
                value = date(int(m.group("year")), month, day)
            except ValueError:
                value = None
        else:
            got = _resolve_year(month, day, published)
            value = got if isinstance(got, date) else None
        weekday = m.group("wd")
        if value is not None and weekday and WEEKDAYS[weekday.lower()] != value.weekday():
            value = None
        out.append((role, value))
    return out


def announced_date(title: object, published_at: datetime | date | None = None) -> date | None:
    """The event date `title` announces, or None when there is none or it is ambiguous. Never raises."""
    try:
        if not isinstance(title, str) or not title.strip():
            return None
        chosen = [d for role, d in dates_in(title, published_at) if role not in ("period_end", "previous")]
        if not chosen:
            return None
        if any(d is None for d in chosen):
            return None
        distinct = set(chosen)
        return distinct.pop() if len(distinct) == 1 else None
    except Exception:  # noqa: BLE001 - a parser never raises into the gate
        return None
