"""Gate 5's Discord earnings-schedule source (DESIGN 2.7, 3.6). Stdlib only; never raises.

Scout stores one `earnings_schedule` fact per (listed date, session, ticker) from the Paper Gains
Discord capture files (open-outcrier "Top Earnings ..." lists, the daily watchlist's "AM ER" / "PM ER",
market-movers' calendar). That text is untrusted and transcribed by an LLM task, so it may only ever
*block* a hold, like SCOUT_SPEC 7.2 item 3 ("any enabled calendar can only add true or unknown"):

- a visible fact naming S whose session falls inside gate 5's window `[as_of, next open)` is evidence
  for `true`;
- nothing here can make gate 5 `false` or clear an `unknown`: no such fact changes nothing.

Session placement on the listed ET date D (the exact time is never known, so each is the widest
honest interval; wider can only block more):
- `pre`: [D 00:00 ET, D's 09:30 open) (the whole day when D is not a trading day);
- `post`: [D's close (16:00, 13:00 on an early close), D 20:00 ET) (the whole day when D is not a
  trading day), so a 19:40 check on the report day still counts it;
- `unknown`: the whole ET day D.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from scout_reader import calendar as cal
from scout_reader._et import et_to_utc
from scout_reader.records import visible

__all__ = ["KIND", "SESSIONS", "schedule_hits", "session_interval"]

KIND = "earnings_schedule"
SESSIONS = ("pre", "post", "unknown")
_POST_END = time(20, 0)


def _at(day: date, t: time) -> datetime:
    return et_to_utc(datetime(day.year, day.month, day.day, t.hour, t.minute)).astimezone(timezone.utc)


def session_interval(day: date, session: str) -> tuple[datetime, datetime]:
    """[start, end) in UTC for a listed earnings session on ET date `day`."""
    whole = (_at(day, time(0, 0)), _at(day + timedelta(days=1), time(0, 0)))
    if session == "pre":
        opening = cal.core_open(day)
        return (whole[0], opening.astimezone(timezone.utc)) if opening is not None else whole
    if session == "post":
        close = cal.core_close(day)
        return (close.astimezone(timezone.utc), _at(day, _POST_END)) if close is not None else whole
    return whole


def schedule_hits(facts: Iterable[Mapping[str, Any]], symbol: str, as_of: datetime, start: datetime,
                  end: datetime) -> list[str]:
    """Evidence ids ("discord:<fact_id>") of visible schedule facts naming `symbol` with a session
    overlapping [start, end). Malformed records are skipped (they can only fail to block)."""
    out: list[str] = []
    for rec in facts:
        try:
            if not isinstance(rec, Mapping) or rec.get("kind") != KIND or not visible(rec, as_of):
                continue
            ticker = rec.get("ticker")
            if not isinstance(ticker, str) or ticker.strip().upper() != symbol:
                continue
            extra = rec.get("extra")
            if not isinstance(extra, Mapping):
                continue
            raw_day, session = extra.get("earnings_date"), extra.get("session")
            if not isinstance(raw_day, str) or session not in SESSIONS:
                continue
            lo, hi = session_interval(date.fromisoformat(raw_day), str(session))
            if lo < end and hi > start:
                out.append(f"discord:{rec.get('fact_id')}")
        except (ValueError, TypeError, OverflowError):
            continue
    return out
