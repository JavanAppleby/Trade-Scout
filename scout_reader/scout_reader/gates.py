"""Gate 6, gate 5 and the headline search, as pure functions over records (SCOUT_SPEC 7.1, 7.2;
DESIGN 3.5, 3.6). Stdlib only. `scout_reader.api` reads the files and calls these; Scout's own
answers job calls the same functions through the api (DESIGN D3), so every bot gets one answer.

Inputs are plain record dicts as stored (DESIGN 5). The caller has already applied the format
check; these functions apply the as-of rule again (cheap, and a pure function must not trust its
caller for look-ahead). `beat` is the governing beat (None means Scout is stale) and `params` the
params record in force at as_of (None means no params). Both missing give `unknown`.

Gate 6 `true` and gate 5 `false` are the two answers that let a hold through, so each needs every
check to pass; anything else is `unknown` (fail closed, SS 2 rule 5). `hold_allowed` is the one
place that says whether a pair of answers allows a hold.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from scout_reader import calendar as cal
from scout_reader._et import utc_to_et
from scout_reader.datetext import announced_date
from scout_reader.records import parse_time, result, visible

__all__ = [
    "CATALYST_KINDS", "NEVER_PASS", "event_before_next_session_from", "fresh_catalyst_from", "gate5_window",
    "gate6_window", "hold_allowed", "macro_events_in", "news_headline_from", "precheck", "trading_day_of",
]

CATALYST_KINDS = ("wire", "filing")
# SS 14: these classes never make gate 6 true, whatever the config says.
NEVER_PASS = frozenset({"offering", "reverse_split", "listing_notice", "other"})
MACRO_KINDS = ("macro_event", "scheduled_event")
_ROLLOVER = time(20, 0)

Result = dict[str, Any]


# --------------------------------------------------------------------------- shared checks


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def precheck(beat: Mapping[str, Any] | None, params: Mapping[str, Any] | None) -> Result | None:
    """`unknown` when Scout is stale or no params are in force; None when the gate may go on."""
    if beat is None:
        return result("unknown", (), True, ("scout_stale",))
    if params is None:
        return result("unknown", (), False, ("no_params",))
    return None


def _names(params: Mapping[str, Any], key: str) -> list[str]:
    value = params.get(key)
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if isinstance(v, str)]


def _int_param(params: Mapping[str, Any], key: str) -> int | None:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _source_problems(beat: Mapping[str, Any], sources: Sequence[str]) -> list[str]:
    """Rule 6: each listed source must be `ok` in the governing beat."""
    statuses = beat.get("sources")
    statuses = statuses if isinstance(statuses, Mapping) else {}
    out: list[str] = []
    for name in sources:
        status = statuses.get(name)
        if status is None:
            out.append(f"source_missing:{name}")
        elif status != "ok":
            out.append(f"source_not_ok:{name}")
    return out


def _ticker_is(rec: Mapping[str, Any], symbol: str) -> bool:
    ticker = rec.get("ticker")
    return isinstance(ticker, str) and ticker.strip().upper() == symbol


def trading_day_of(dt: datetime) -> date:
    """The trading day `dt` belongs to (20:00 ET rollover, closed days roll forward; Guardian's rule)."""
    et = utc_to_et(dt)
    day = et.date()
    if et.time() >= _ROLLOVER or not cal.is_trading_day(day):
        return cal.next_trading_day(day)
    return day


def hold_allowed(fresh_catalyst: object, event_before_next_session: object) -> bool:
    """True only for (gate 6 `true`, gate 5 `false`), both fresh and well formed (SS 14). Any
    other pair, a stale answer or a malformed one never allows a hold."""
    def ok(res: object, want: str) -> bool:
        return (isinstance(res, Mapping) and res.get("value") == want and res.get("stale") is False
                and isinstance(res.get("reasons"), list))
    return ok(fresh_catalyst, "true") and ok(event_before_next_session, "false")


# --------------------------------------------------------------------------- gate 6


def gate6_window(as_of: datetime) -> tuple[datetime, datetime]:
    """[prior trading day's close (16:00 ET, 13:00 on an early close), as_of] in UTC. "Prior" is
    taken from the trading day `as_of` belongs to (20:00 ET rollover), so the 19:40 hold check on a
    Monday looks back to Friday's close, not to Monday's own 16:00 (SS 7.1)."""
    day = cal.previous_trading_day(trading_day_of(as_of))
    close = cal.core_close(day)
    assert close is not None  # previous_trading_day only returns trading days
    return close.astimezone(timezone.utc), as_of.astimezone(timezone.utc)


def _in(at: datetime | None, start: datetime, end: datetime) -> bool:
    return at is not None and start <= at <= end


def _catalysts(facts: Iterable[Mapping[str, Any]], symbol: str, start: datetime,
               as_of: datetime) -> list[Mapping[str, Any]]:
    out = []
    for rec in facts:
        if rec.get("kind") not in CATALYST_KINDS or not _ticker_is(rec, symbol) or not visible(rec, as_of):
            continue
        if _in(parse_time(rec.get("published_at")), start, as_of) and _in(
                parse_time(rec.get("first_seen_at")), start, as_of):
            out.append(rec)
    return out


def _coverage_gaps() -> Callable[..., Any] | None:
    try:
        module = importlib.import_module("scout_reader.coverage")
    except ImportError:
        return None
    fn = getattr(module, "gaps", None)
    return fn if callable(fn) else None


def _coverage_problems(health: Sequence[Mapping[str, Any]], beat: Mapping[str, Any], sources: Sequence[str],
                       start: datetime, end: datetime, max_gap_s: int | None, feeds: object = None) -> list[str]:
    """Why `false` cannot be trusted over [start, end]: a gap or a poll without overlap on any
    gate-6 source (SS 6.2), an open gap in the beat, or no coverage rule to ask. `feeds` is params
    `gate6_feeds` ({source: [feed, ...]}): a listed feed with no health record is a gap."""
    if not sources:
        return ["no_gate6_sources"]
    if max_gap_s is None:
        return ["no_param:source_max_gap_s"]
    gaps_fn = _coverage_gaps()
    if gaps_fn is None:
        return ["not_built:coverage"]
    gap_open = beat.get("gap_open")
    gap_open = gap_open if isinstance(gap_open, Mapping) else {}
    expected = feeds if isinstance(feeds, Mapping) else {}
    out: list[str] = []
    for name in sources:
        if gap_open.get(name) is True:
            out.append(f"gap_open:{name}")
        want = expected.get(name)
        if isinstance(want, list) and all(isinstance(f, str) for f in want):
            found = gaps_fn(list(health), name, start, end, max_gap_s=max_gap_s, feeds=want)
        else:
            found = gaps_fn(list(health), name, start, end, max_gap_s=max_gap_s)
        if not isinstance(found, list):
            out.append(f"coverage_error:{name}")
            continue
        for gap in found:
            why = gap.get("why", "gap") if isinstance(gap, Mapping) else "gap"
            out.append(f"gap:{name}:{why}")
    return out


def _search(facts: Iterable[Mapping[str, Any]], health: Sequence[Mapping[str, Any]], beat: Mapping[str, Any] | None,
            params: Mapping[str, Any] | None, symbol: str, as_of: datetime,
            classes: frozenset[str] | None) -> Result:
    early = precheck(beat, params)
    if early is not None:
        return early
    assert beat is not None and params is not None
    start, end = gate6_window(as_of)
    window = [_iso(start), _iso(end)]
    sources = sorted(set(_names(params, "gate6_sources")))
    problems = _source_problems(beat, sources)
    if problems:
        out = result("unknown", (), False, problems)
        out["window"] = window
        return out
    found = _catalysts(facts, symbol, start, end)
    matched = [f for f in found if classes is None or (f.get("class") in classes)]
    if matched:
        matched.sort(key=lambda f: (str(f.get("first_seen_at")), str(f.get("fact_id"))))
        out = result("true", [str(f.get("fact_id")) for f in matched], False, ("fact_in_window",))
        out["window"] = window
        out["classes"] = sorted({str(f.get("class")) for f in matched})
        return out
    gaps = _coverage_problems(health, beat, sources, start, end, _int_param(params, "source_max_gap_s"),
                              params.get("gate6_feeds"))
    out = result("unknown" if gaps else "false", (), False, gaps or ("no_fact_full_coverage",))
    out["window"] = window
    out["classes"] = []
    return out


def fresh_catalyst_from(facts: Iterable[Mapping[str, Any]], health: Sequence[Mapping[str, Any]],
                        beat: Mapping[str, Any] | None, params: Mapping[str, Any] | None, symbol: str,
                        as_of: datetime) -> Result:
    """Gate 6 (SS 7.1, DESIGN 3.5)."""
    classes = frozenset(_names(params or {}, "hold_catalyst_classes")) - NEVER_PASS
    res = _search(facts, health, beat, params, symbol, as_of, classes)
    res.pop("classes", None)
    return res


def news_headline_from(facts: Iterable[Mapping[str, Any]], health: Sequence[Mapping[str, Any]],
                       beat: Mapping[str, Any] | None, params: Mapping[str, Any] | None, symbol: str,
                       as_of: datetime) -> Result:
    """Any wire or filing fact naming S in gate 6's window, whatever its class; `classes` lists them."""
    res = _search(facts, health, beat, params, symbol, as_of, None)
    res.setdefault("classes", [])
    return res


# --------------------------------------------------------------------------- gate 5


def gate5_window(as_of: datetime) -> tuple[datetime, datetime] | None:
    """[as_of, next trading day's 09:30 ET open) in UTC; None when the calendar does not cover it."""
    opening = cal.next_open_after(as_of)
    if opening is None:
        return None
    return as_of.astimezone(timezone.utc), opening


def _announced(facts: Iterable[Mapping[str, Any]], symbol: str, as_of: datetime, lookback_days: int,
               first: date, last: date) -> tuple[list[str], list[str]]:
    """(fact ids whose announced date falls on an ET day of the window, fact ids not parsed)."""
    since = as_of - timedelta(days=lookback_days)
    hits: list[str] = []
    unparsed: list[str] = []
    for rec in facts:
        if rec.get("class") != "earnings_date" or not _ticker_is(rec, symbol) or not visible(rec, as_of):
            continue
        published = parse_time(rec.get("published_at"))
        if published is None or published < since:
            continue
        day = announced_date(rec.get("title"), published)
        fid = str(rec.get("fact_id"))
        if day is None:
            unparsed.append(fid)
        elif first <= day <= last:
            hits.append(fid)
    return hits, unparsed


def _due_visible(rec: Mapping[str, Any], as_of: datetime) -> datetime | None:
    at = parse_time(rec.get("computed_at"))
    return at if at is not None and at <= as_of else None


def _due_problems(due: Iterable[Mapping[str, Any]], symbol: str, as_of: datetime) -> tuple[list[str], list[str]]:
    """(reasons the due window gives `unknown`, evidence). Empty reasons: S is outside its window."""
    today = trading_day_of(as_of)
    best: Mapping[str, Any] | None = None
    best_at: datetime | None = None
    for rec in due:
        if not _ticker_is(rec, symbol):
            continue
        at = _due_visible(rec, as_of)
        if at is None or trading_day_of(at) != today:
            continue
        if best_at is None or at >= best_at:
            best, best_at = rec, at
    if best is None:
        return ["no_due_record"], []
    evidence = [f"due:{best.get('computed_at')}"]
    status = best.get("status")
    if status != "ok":
        return [f"due_status:{status}"], evidence
    if best.get("nt_recent") is not False:
        return ["nt_recent" if best.get("nt_recent") else "due_field:nt_recent"], evidence
    if best.get("overdue") is not False:
        return ["overdue" if best.get("overdue") else "due_field:overdue"], evidence
    try:
        start = date.fromisoformat(str(best.get("due_start")))
        end = date.fromisoformat(str(best.get("due_end")))
    except ValueError:
        return ["due_field:window"], evidence
    first = utc_to_et(as_of).date()
    last = cal.next_trading_day(first)
    if first > end:
        return ["overdue"], evidence
    if first <= end and last >= start:
        return ["in_due_window"], evidence + [f"due_window:{start.isoformat()}..{end.isoformat()}"]
    return [], evidence + [f"due_window:{start.isoformat()}..{end.isoformat()}"]


def macro_events_in(facts: Iterable[Mapping[str, Any]], as_of: datetime, start: datetime,
                    end: datetime) -> list[Mapping[str, Any]]:
    """Visible macro or scheduled event facts with `event_at` in [start, end)."""
    out = []
    for rec in facts:
        if rec.get("kind") not in MACRO_KINDS or not visible(rec, as_of):
            continue
        at = parse_time(rec.get("event_at"))
        if at is not None and start <= at < end:
            out.append(rec)
    return out


def event_before_next_session_from(facts: Iterable[Mapping[str, Any]], due: Iterable[Mapping[str, Any]],
                                   beat: Mapping[str, Any] | None, params: Mapping[str, Any] | None,
                                   symbol: str, as_of: datetime) -> Result:
    """Gate 5 (SS 7.2, DESIGN 3.6). `true` when any source finds an event in the window; `false`
    only when no announced date, S outside its EDGAR due window, the gate-6 sources ok, Scout fresh
    and the calendar covering the next session; `unknown` otherwise."""
    early = precheck(beat, params)
    if early is not None:
        return early
    assert beat is not None and params is not None
    facts = list(facts)
    window = gate5_window(as_of)
    if window is None:
        return result("unknown", (), False, ("calendar_not_covered",))
    start, end = window
    out_window = [_iso(start), _iso(end)]
    first, last = utc_to_et(start).date(), utc_to_et(end).date()
    lookback = _int_param(params, "earnings_lookback_days")
    reasons: list[str] = []
    hits: list[str] = []
    unparsed: list[str] = []
    if lookback is None:
        reasons.append("no_param:earnings_lookback_days")
    else:
        hits, unparsed = _announced(facts, symbol, as_of, lookback, first, last)
    if params.get("hold_block_macro_events") is True:
        hits += [f"macro:{f.get('fact_id')}" for f in macro_events_in(facts, as_of, start, end)]
    elif params.get("hold_block_macro_events") is not False:
        reasons.append("no_param:hold_block_macro_events")
    if hits:
        res = result("true", sorted(set(hits)), False, ("event_in_window",))
        res["window"] = out_window
        return res
    reasons += [f"unparsed_date:{fid}" for fid in unparsed]
    reasons += _source_problems(beat, sorted(set(_names(params, "gate6_sources"))))
    due_reasons, evidence = _due_problems(due, symbol, as_of)
    reasons += due_reasons
    res = result("unknown" if reasons else "false", evidence, False, reasons or ("outside_due_window",))
    res["window"] = out_window
    return res
