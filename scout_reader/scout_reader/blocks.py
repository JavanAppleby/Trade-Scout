"""`entry_blocks`: is there known bad news on file that should stop Radar's buy signal for a stock?
(owner, 2026-11-04; SCOUT_SPEC 7.8, 10.3; DESIGN 3.9). Stdlib only; never raises into the caller.

Scout can only stop a buy, never start one. The blocks, each with its window:

| kind | on file when |
|---|---|
| `offering`, `reverse_split`, `listing_notice`, `bankruptcy` | a wire or filing fact naming the stock with that class, first seen within the last `lookback_days` trading days (counted back from as_of's trading day, 20:00 ET rollover) |
| `halt_open` | a halt of the stock that began within the lookback window and has no resumption by as_of |
| `macro_window` | a scheduled CPI release, FOMC decision or jobs report with `event_at` in [as_of - after, as_of + before] |

Every record must be visible at as_of (`first_seen_at` and `published_at` <= as_of, SS 2 rule 9).
Discord posts, scanner cards and LLM labels never count (decision C; SS 2 rule 7). A 424B2 filing
never counts as an offering, and an 8-K with item 1.03 always counts as a bankruptcy, also when it was
stored with another class before the 2026-11-04 class fix.

The answer: `true` when any block is on file, **even when Scout is stale or a source is not ok**
(known bad news always blocks). Otherwise `false` only when Scout is fresh, the params are in force,
every gate-6 source is ok with no gap over the lookback window (SS 6.2), the halt feed is ok with no
gap open, no halt row was skipped in the window, the macro calendar is fresh (nowflation,
or BLS with the FOMC table reaching past the window), and no file read had a damaged line;
`unknown` otherwise. Staleness only ever turns `false` into `unknown`. Radar treats `unknown` as
"Scout unknown" and lets the buy go ahead (decision B).

Files are read through the Reader's `FileCache`, so repeated calls cost little: the facts view keeps
only the records a block can come from, the health view keeps each poll as a `coverage.Poll`.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from scout_reader import calendar as cal
from scout_reader import coverage as cov
from scout_reader import macro as M
from scout_reader import records as R
from scout_reader._et import et_to_utc
from scout_reader.cache import FileCache, View
from scout_reader.gates import CATALYST_KINDS, trading_day_of
from scout_reader.gates import _source_problems  # noqa: PLC2701 - the same package's rule 6
from scout_reader.halts import SOURCE as HALT_SOURCE
from scout_reader.halts import open_halts

__all__ = [
    "BLOCK_CLASSES", "BLOCK_KINDS", "DEFAULT_LOOKBACK_DAYS", "DEFAULT_MACRO_AFTER_MIN", "DEFAULT_MACRO_BEFORE_MIN",
    "MACRO_EVENT_KINDS", "NOT_OFFERING_FORMS", "entry_blocks_in", "find_beat", "lookback_start",
]

BLOCK_CLASSES = ("offering", "reverse_split", "listing_notice", "bankruptcy")
BLOCK_KINDS = (*BLOCK_CLASSES, "halt_open", "macro_window")
MACRO_EVENT_KINDS = ("cpi", "fomc", "nfp")  # nfp: the jobs report
NOT_OFFERING_FORMS = ("424B2",)
DEFAULT_LOOKBACK_DAYS = 3  # decision A
DEFAULT_MACRO_BEFORE_MIN = 5  # decision D
DEFAULT_MACRO_AFTER_MIN = 15
_ROLLOVER = time(20, 0)
_POLL_TAIL = timedelta(hours=1)  # polls that started this close to as_of may have ended after it
_SKIPPED = "skipped "  # how `scout.halts` starts the health `error` of an ok poll that skipped bad rows

Result = dict[str, Any]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def lookback_start(as_of: datetime, days: int) -> tuple[date, datetime]:
    """(the first trading day of the window, the UTC instant it begins). The window is as_of's trading
    day and the `days - 1` trading days before it; a trading day begins at 20:00 ET of the trading
    day before it (the rollover `gates.trading_day_of` uses)."""
    first = trading_day_of(as_of)
    for _ in range(max(1, int(days)) - 1):
        first = cal.previous_trading_day(first)
    before = cal.previous_trading_day(first)
    return first, et_to_utc(datetime.combine(before, _ROLLOVER))


# --------------------------------------------------------------------------- cached views


def _block_class(rec: Mapping[str, Any]) -> str | None:
    """The blocking class of a wire or filing fact, re-checked against the 2026-11-04 class fix for
    facts stored before it: a 424B2 is never an offering, an 8-K with item 1.03 is a bankruptcy."""
    kind = rec.get("kind")
    if kind not in CATALYST_KINDS:
        return None
    items = rec.get("items")
    if kind == "filing" and isinstance(items, list) and "1.03" in items:
        return "bankruptcy"
    cls = rec.get("class")
    if cls not in BLOCK_CLASSES:
        return None
    if cls == "offering" and str(rec.get("form") or "").strip().upper().startswith(NOT_OFFERING_FORMS):
        return None
    return str(cls)


def _fact_item(rec: dict[str, Any]) -> dict[str, Any] | None:
    """The facts a block can come from: wire and filing facts with a blocking class, halts."""
    if not R.known(rec):
        return None
    if rec.get("kind") == "halt" or _block_class(rec) is not None:
        return rec
    return None


def _ticker(rec: Mapping[str, Any]) -> str:
    ticker = rec.get("ticker")
    return ticker.strip().upper() if isinstance(ticker, str) else ""


def _poll_item(rec: dict[str, Any]) -> tuple[Any, ...] | None:
    """A `coverage.Poll` followed by (end, source, feed, skipped bad rows), for a health record with
    readable times."""
    if not R.known(rec):
        return None
    at, end = R.parse_time(rec.get("start")), R.parse_time(rec.get("end"))
    if at is None or end is None:
        return None
    error = rec.get("error")
    skipped = rec.get("result") == "ok" and isinstance(error, str) and error.startswith(_SKIPPED)
    return (*cov.poll_of(at, rec), end, str(rec.get("source") or ""), str(rec.get("feed") or ""), skipped)


def _beat_item(rec: dict[str, Any]) -> tuple[datetime, dict[str, Any]] | None:
    at = R.parse_time(rec.get("at"))
    return None if at is None else (at, rec)


def _history_item(rec: dict[str, Any]) -> dict[str, Any] | None:
    return rec if R.known(rec) and rec.get("source") in M.SOURCES else None


class _Files:
    """The views one answer reads, with the damage notes of every file it touched (rule 4)."""

    def __init__(self, root: Path, cache: FileCache) -> None:
        self.root, self.cache = root, cache
        self.notes: set[str] = set()

    def view(self, path: Path, label: str, name: str, project: Any, key: Any, order: Any = None) -> View | None:
        try:
            got = self.cache.load(path, name, project, key, order)
        except OSError:
            self.notes.add(f"unreadable_file:{label}")
            return None
        if got is not None:
            self.notes.update(f"{n}:{label}" for n in got.notes())
        return got

    def days(self, folder: str, start: datetime, end: datetime, name: str, project: Any, key: Any,
             order: Any = None) -> list[View]:
        out = []
        for path in R.day_files(self.root / folder, start, end):
            got = self.view(path, f"{folder.rsplit('/', 1)[-1]}/{path.name}", name, project, key, order)
            if got is not None:
                out.append(got)
        return out


def find_beat(root: Path, cache: FileCache, as_of: datetime, stale_s: float) -> tuple[dict[str, Any] | None, list[str]]:
    """`Reader._beat` over the cache: the latest beat in (as_of - stale_s, as_of], or None (stale)."""
    start = as_of - timedelta(seconds=stale_s)
    best: dict[str, Any] | None = None
    best_at: datetime | None = None
    files = _Files(root, cache)
    for view in files.days(R.BEATS, start, as_of, "beats", _beat_item, lambda _i: "", order=lambda i: i[0]):
        items = view.groups.get("", [])
        lo = bisect.bisect_right(items, start, key=lambda i: i[0])
        hi = bisect.bisect_right(items, as_of, key=lambda i: i[0])
        for at, rec in items[lo:hi]:
            if best_at is None or at >= best_at:
                best, best_at = rec, at
    if best is None:
        return None, ["scout_stale"]
    if not R.known(best):
        return None, ["scout_stale", "unknown_format:beats"]
    if best.get("stopped") is not False:
        return None, ["scout_stale", "scout_stopped"]
    return best, []


# --------------------------------------------------------------------------- the blocks


def _block(kind: str, cls: object, fact_id: object, first_seen_at: object, title: object) -> dict[str, Any]:
    return {"kind": kind, "class": None if cls is None else str(cls), "fact_id": None if fact_id is None else str(fact_id),
            "first_seen_at": None if first_seen_at is None else str(first_seen_at),
            "title": None if title is None else str(title)[:300]}


def _fact_blocks(facts: Iterable[Mapping[str, Any]], as_of: datetime, first: date) -> list[dict[str, Any]]:
    out = []
    for rec in facts:
        cls = _block_class(rec)
        if cls is None or not R.visible(rec, as_of):
            continue
        seen = R.parse_time(rec.get("first_seen_at"))
        if seen is None or trading_day_of(seen) < first:
            continue
        out.append(_block(cls, cls, rec.get("fact_id"), rec.get("first_seen_at"), rec.get("title")))
    return out


def _halt_blocks(facts: Iterable[Mapping[str, Any]], symbol: str, as_of: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    """(a `halt_open` block for every halt of S in the facts read with no resumption by as_of, the ET
    dates of those that began before as_of's ET date). An older one blocks too (known bad news on
    file); its date is kept as a reason so a missed resumption can be seen."""
    open_today, open_older = open_halts(facts, symbol, as_of)
    out = []
    for key, recs in open_today + [(key, recs) for _d, key, recs in open_older]:
        rec = min(recs, key=lambda r: str(r.get("first_seen_at")))
        out.append(_block("halt_open", rec.get("halt_code"), rec.get("fact_id") or key, rec.get("first_seen_at"),
                          f"halted {rec.get('halt_time')} ({rec.get('halt_code') or 'no code'})"))
    return out, sorted({d for d, _k, _r in open_older})


def _macro(history: Sequence[Mapping[str, Any]], params: Mapping[str, Any] | None, beat: Any, as_of: datetime,
           lo: datetime, hi: datetime) -> tuple[list[dict[str, Any]], list[str]]:
    """(macro blocks in [lo, hi], why `false` cannot be trusted for the macro calendar)."""
    latest = M._latest(history, as_of)  # noqa: SLF001 - the same package's macro rules
    params = params or {}
    seen = {s: rec.get("first_seen_at") for s, rec in latest.items()}
    seen["fomc"] = params.get("first_seen_at")
    out = []
    for at, kind, source in M._events(latest, None, params, as_of):  # noqa: SLF001
        if kind in MACRO_EVENT_KINDS and lo <= at <= hi:
            out.append(_block("macro_window", kind, f"{kind}@{_iso(at)}({source})", seen.get(source),
                              f"{kind} at {_iso(at)} ({source})"))
    now_ok, now_why = M._fresh(latest.get("nowflation"), as_of, beat, "nowflation")  # noqa: SLF001
    if now_ok:
        return out, []
    bls_ok, bls_why = M._fresh(latest.get("bls"), as_of, beat, "bls")  # noqa: SLF001
    fomc = M._fomc_dates(params)  # noqa: SLF001
    table_ok = bool(fomc) and M._fomc_at(max(fomc), params) >= hi  # noqa: SLF001
    if bls_ok and table_ok:
        return out, []
    return out, [r for r in (now_why, bls_why) if r] + ([] if table_ok else ["fomc_table_short"])


def _coverage(files: _Files, params: Mapping[str, Any], beat: Mapping[str, Any], start: datetime,
              as_of: datetime) -> list[str]:
    """`gates._coverage_problems` over cached polls: every gate-6 source ok with no gap over [start, as_of].
    Also `halt_rows_skipped` when a halt poll in [start, as_of] skipped a bad row: that row may have
    been the stock's halt, so the halt feed did not cover it."""
    sources = sorted({str(s) for s in params.get("gate6_sources") or () if isinstance(s, str)})
    if not sources:
        return ["no_gate6_sources"]
    out = _source_problems(beat, sources)
    max_gap = params.get("source_max_gap_s")
    if isinstance(max_gap, bool) or not isinstance(max_gap, (int, float)):
        return out + ["no_param:source_max_gap_s"]
    chains: dict[tuple[str, str], list[list[tuple[Any, ...]]]] = {}
    skipped = False
    for view in files.days(R.HEALTH, start - timedelta(days=1), as_of, "polls", _poll_item,
                           lambda i: (i[5], i[6]), order=lambda i: i[0]):
        for k, items in view.groups.items():
            chains.setdefault(k, []).append(items)
            if k[0] == HALT_SOURCE:
                skipped = skipped or any(i[7] and i[0] >= start and i[4] <= as_of for i in items)
    gap_open = beat.get("gap_open") if isinstance(beat.get("gap_open"), Mapping) else {}
    feeds_wanted = params.get("gate6_feeds") if isinstance(params.get("gate6_feeds"), Mapping) else {}
    for name in sources:
        if gap_open.get(name) is True:
            out.append(f"gap_open:{name}")
        feeds = {f: parts for (s, f), parts in chains.items() if s == name}
        want = feeds_wanted.get(name)
        if isinstance(want, list) and any(str(f) not in feeds for f in want):
            out.append(f"gap:{name}:missing_feed")
        if not feeds:
            out.append(f"gap:{name}:no_poll_at_start")
        for feed, parts in feeds.items():
            if isinstance(want, list) and want and feed not in {str(f) for f in want}:
                continue  # a feed gate 6 does not read, e.g. EDGAR's 10-K (owner, 2026-11-04)
            for gap in cov.chain_gaps(_chain(parts, as_of), start, as_of, float(max_gap)):
                out.append(f"gap:{name}:{gap['why']}")
    if skipped:
        out.append("halt_rows_skipped")
    return list(dict.fromkeys(out))


def _chain(parts: list[list[tuple[Any, ...]]], as_of: datetime) -> list[tuple[Any, ...]]:
    """One feed's polls from several day files, in start order, ended at or before as_of."""
    merged: list[tuple[Any, ...]] = []
    for items in parts:
        if merged and items and items[0][0] < merged[-1][0]:
            merged = sorted(merged + items, key=lambda i: i[0])
        else:
            merged.extend(items)
    stop = bisect.bisect_right(merged, as_of, key=lambda i: i[0])
    tail = bisect.bisect_left(merged, as_of - _POLL_TAIL, 0, stop, key=lambda i: i[0])
    return merged[:tail] + [i for i in merged[tail:stop] if i[4] <= as_of]


def entry_blocks_in(root: Path, cache: FileCache, params: Mapping[str, Any] | None, p_reasons: Sequence[str],
                    beat: Mapping[str, Any] | None, b_reasons: Sequence[str], symbol: str, as_of: datetime,
                    lookback_days: int, before_min: float, after_min: float) -> Result:
    """The answer for an upper-cased `symbol` at an aware `as_of` (see the module docstring)."""
    first, start = lookback_start(as_of, lookback_days)
    lo, hi = as_of - timedelta(minutes=after_min), as_of + timedelta(minutes=before_min)
    files = _Files(root, cache)
    facts: list[Mapping[str, Any]] = []
    for view in files.days(R.FACTS, start, as_of, "blocks", _fact_item, _ticker):
        facts.extend(view.groups.get(symbol, ()))
    blocks = _fact_blocks(facts, as_of, first)
    halts, older = _halt_blocks(facts, symbol, as_of)
    blocks += halts
    history_view = files.view(root / R.MACRO_HISTORY, "macro/history.jsonl", "history", _history_item, lambda _i: "")
    history = history_view.groups.get("", []) if history_view is not None else []
    macro, macro_why = _macro(history, params, beat, as_of, lo, hi)
    blocks += macro
    blocks.sort(key=lambda b: (BLOCK_KINDS.index(b["kind"]), str(b["first_seen_at"]), str(b["fact_id"])))
    if blocks:
        why = ["block_on_file"] + (["scout_stale"] if beat is None else []) + [f"halt_unresolved_since:{d}" for d in older]
        res = R.result("true", [str(b["fact_id"]) for b in blocks], beat is None, why)
    else:
        reasons = list(b_reasons) + list(p_reasons)
        if beat is not None and params is not None:
            reasons += _coverage(files, params, beat, start, as_of)
            statuses = beat.get("sources") if isinstance(beat.get("sources"), Mapping) else {}
            gap_open = beat.get("gap_open") if isinstance(beat.get("gap_open"), Mapping) else {}
            if statuses.get(HALT_SOURCE) != "ok":
                reasons.append(f"source_not_ok:{HALT_SOURCE}")
            elif gap_open.get(HALT_SOURCE) is True:
                reasons.append(f"gap_open:{HALT_SOURCE}")
            reasons += macro_why
            reasons += sorted(files.notes)  # rule 4: a damaged file turns false into unknown
        reasons = list(dict.fromkeys(reasons))
        res = R.result("unknown" if reasons else "false", (), beat is None, reasons or ("no_block_full_coverage",))
    res["blocks"] = blocks
    res["window"] = {"facts_from": _iso(start), "first_trading_day": first.isoformat(),
                     "macro": [_iso(lo), _iso(hi)]}
    return res
