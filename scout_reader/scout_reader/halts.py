"""`halt_open` over Scout's halt facts (SCOUT_SPEC 7.5; DESIGN 3.8). Pure, stdlib only, never raises.

`halt_open_from(facts, beat, symbol, as_of)`:
- `unknown` when Scout is stale (no governing beat), when `nasdaq_halts` is not `ok` in that beat
  or has a gap open (a stale feed means unknown, never "not halted"), or when an open halt for the
  symbol started before as_of's ET date (it may have resumed while the feed no longer listed it);
- `true` when a visible halt fact for the symbol, halted on as_of's ET date at or before as_of, has
  no visible resumption at or before as_of;
- `false` otherwise.

Halt facts group by `extra.halt_key` (symbol|HaltDate|HaltTime): the first-seen fact and any later
resumption fact share it. A scheduled resumption later than as_of does not count yet. Record only:
no bot gates on halts in v1.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from scout_reader import records as R
from scout_reader._et import utc_to_et

SOURCE = "nasdaq_halts"
LOOKBACK_DAYS = 7  # how many ET days of facts files `load_halt_facts` reads

__all__ = ["LOOKBACK_DAYS", "SOURCE", "halt_open_at", "halt_open_from", "load_halt_facts"]


def _aware(as_of: Any) -> bool:
    return isinstance(as_of, datetime) and as_of.tzinfo is not None and as_of.utcoffset() is not None


def _key(rec: Mapping[str, Any]) -> str:
    extra = rec.get("extra")
    if isinstance(extra, Mapping) and isinstance(extra.get("halt_key"), str):
        return extra["halt_key"]
    guid = str(rec.get("guid") or "")
    return guid.split("|resumed|")[0]


def _source_reason(beat: Mapping[str, Any]) -> str | None:
    sources = beat.get("sources")
    status = sources.get(SOURCE) if isinstance(sources, Mapping) else None
    if status != "ok":
        return f"source_not_ok:{SOURCE}"
    gaps = beat.get("gap_open")
    if isinstance(gaps, Mapping) and gaps.get(SOURCE) is True:
        return f"gap_open:{SOURCE}"
    return None


def halt_open_from(facts: Iterable[Mapping[str, Any]], beat: Mapping[str, Any] | None, symbol: str,
                   as_of: datetime) -> dict[str, Any]:
    try:
        return _halt_open(facts, beat, symbol, as_of)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - the reader never raises into its caller
        return R.UNKNOWN(f"error:{type(exc).__name__}")


def _halt_open(facts: Iterable[Mapping[str, Any]], beat: Mapping[str, Any] | None, symbol: str,
               as_of: datetime) -> dict[str, Any]:
    if not _aware(as_of):
        return R.UNKNOWN("naive_as_of")
    sym = str(symbol or "").strip().upper()
    if not sym:
        return R.UNKNOWN("no_symbol")
    if beat is None:
        return R.UNKNOWN("scout_stale", stale=True)
    why = _source_reason(beat)
    if why is not None:
        return R.UNKNOWN(why)
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for rec in facts or ():
        if not isinstance(rec, Mapping) or not R.known(rec) or rec.get("kind") != "halt":
            continue
        if str(rec.get("ticker") or "").strip().upper() != sym or not R.visible(rec, as_of):
            continue
        groups.setdefault(_key(rec), []).append(rec)
    today = utc_to_et(as_of).date()
    open_today: list[str] = []
    open_older: list[str] = []
    for key, recs in sorted(groups.items()):
        halted = [R.parse_time(r.get("halt_time")) for r in recs]
        halted_at = min((h for h in halted if h is not None), default=None)
        if halted_at is None or halted_at > as_of:
            continue
        resumed = any((t := R.parse_time(r.get("resumed_at"))) is not None and t <= as_of for r in recs)
        if resumed:
            continue
        ids = [str(r.get("fact_id")) for r in recs if r.get("fact_id")]
        if utc_to_et(halted_at).date() == today:
            open_today.extend(ids or [key])
        else:
            open_older.append(utc_to_et(halted_at).date().isoformat())
    if open_today:
        return R.result("true", evidence=open_today)
    if open_older:
        return R.result("unknown", reasons=[f"halt_unresolved_since:{d}" for d in sorted(set(open_older))])
    return R.result("false")


def load_halt_facts(scout_dir: str | os.PathLike[str], as_of: datetime,
                    lookback_days: int = LOOKBACK_DAYS) -> list[dict[str, Any]]:
    """Halt facts from the ET-date facts files covering [as_of - lookback_days, as_of]."""
    out: list[dict[str, Any]] = []
    start = as_of - timedelta(days=lookback_days)
    for path in R.day_files(os.path.join(os.fspath(scout_dir), R.FACTS), start, as_of):
        out.extend(rec for rec in R.read_jsonl(path) if rec.get("kind") == "halt")
    return out


def halt_open_at(scout_dir: str | os.PathLike[str], symbol: str, as_of: datetime,
                 stale_s: float | None = None) -> dict[str, Any]:
    """Convenience for `scout_reader.api`: reads the beat (stale_s, else params.scout_stale_s, else
    120) and the facts, then `halt_open_from`. Never raises."""
    try:
        if not _aware(as_of):
            return R.UNKNOWN("naive_as_of")
        if stale_s is None:
            params = R.last_params(scout_dir, as_of) or {}
            value = params.get("scout_stale_s")
            stale_s = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else R.DEFAULT_STALE_S
        beat = R.governing_beat(scout_dir, as_of, stale_s)
        return halt_open_from(load_halt_facts(scout_dir, as_of), beat, symbol, as_of)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001
        return R.UNKNOWN(f"error:{type(exc).__name__}")
