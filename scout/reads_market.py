"""What a reader is shown of the market (0.3.2; owner, 2026-11-11; SCOUT_SPEC 19.5, DESIGN 24).

The market feed (`scout.market_feed`) appends `state/reads/market/<ET date>.jsonl`: one `market` line
per poll that gave usable prices, one `miss` line per poll that sent a request and recorded nothing.
This module reads that file for the reads only (nothing that answers a question reads it):

- `latest_record(paths, now)`: the newest `market` line of `now`'s ET day first seen by `now` (no
  look-ahead); bad lines are skipped; the parsed file is cached on its size and modification time.
- `from_record` / `from_pack`: Scout's record or the pack's own `market` block as one shape,
  `{"source": "scout"|"radar", "feed": str|None, "at": iso, "symbols": {SYM: {"last", "change_pct"}}}`
  (`change_pct` a fraction, as READS_TEST.md 3 keeps ratios).
- `shown_market(pack_market, rec, now, max_age_s)`: the block a read is shown: whichever of the two
  is newer, each only when its `at` is at most `max_age_s` old (and not in the future); else None.
- `market_line(block, now, today)`: the prompt's line, "Market at 10:01:58 ET: SPY 681.20 +0.31%; ...",
  with "(15 minutes before now)" once the data is 5 minutes old or more, or "Market: not available".
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scout.clock import et_date, parse_iso
from scout.paths import ScoutPaths
from scout.store import read_jsonl

__all__ = ["AGE_SHOWN_FROM_S", "FUTURE_TOLERANCE_S", "from_pack", "from_record", "latest_record", "market_file",
           "market_line", "market_lines", "shown_market"]

AGE_SHOWN_FROM_S = 300  # from 5 minutes old the line says how old the data is
FUTURE_TOLERANCE_S = 60  # as the market feed's: a PC clock a little behind Alpaca's
_cache: dict[str, tuple[tuple[int, int], list[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()


def market_file(paths: ScoutPaths, day: date | str) -> Path:
    return paths.reads / "market" / f"{day if isinstance(day, str) else day.isoformat()}.jsonl"


def market_lines(paths: ScoutPaths, day: date | str) -> list[dict[str, Any]]:
    """Every parseable line of the day's market file ([] when there is none), cached on (size, mtime)."""
    path = market_file(paths, day)
    try:
        st = path.stat()
    except OSError:
        return []
    stamp = (st.st_size, st.st_mtime_ns)
    key = str(path)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1]
    rows = read_jsonl(path)
    with _cache_lock:
        if len(_cache) > 8:
            _cache.clear()
        _cache[key] = (stamp, rows)
    return rows


def latest_record(paths: ScoutPaths, now: datetime) -> dict[str, Any] | None:
    """The newest usable `market` line of `now`'s ET day first seen at or before `now`."""
    best: tuple[datetime, dict[str, Any]] | None = None
    for rec in market_lines(paths, et_date(now)):
        if rec.get("kind") != "market" or from_record(rec) is None:
            continue
        seen = parse_iso(rec.get("first_seen_at"))
        if seen is None or seen > now:
            continue
        if best is None or seen >= best[0]:
            best = (seen, rec)
    return best[1] if best is not None else None


def _number(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return str(value).strip() if num.is_finite() else None


def _symbols(given: Mapping[str, Any], order: Iterable[str] = ()) -> dict[str, dict[str, str]] | None:
    out: dict[str, dict[str, str]] = {}
    for sym, row in given.items():
        if not isinstance(row, Mapping):
            continue
        last, change = _number(row.get("last")), _number(row.get("change_pct"))
        if last is None or Decimal(last) <= 0 or change is None:
            continue  # a row without a price and a change is left out
        out[str(sym)] = {"last": last, "change_pct": change}
    if not out:
        return None
    first = [s for s in order if s in out]
    return {s: out[s] for s in [*first, *(s for s in out if s not in first)]}


def from_record(rec: Any, order: Iterable[str] = ()) -> dict[str, Any] | None:
    """Scout's `market` line as the shown block, or None when it is not usable."""
    if not isinstance(rec, Mapping) or rec.get("kind") != "market" or not isinstance(rec.get("symbols"), Mapping):
        return None
    at = parse_iso(rec.get("at"))
    symbols = _symbols(rec["symbols"], order)
    if at is None or symbols is None:
        return None
    feed = rec.get("feed")
    return {"source": "scout", "feed": feed if isinstance(feed, str) else None, "at": rec.get("at"), "symbols": symbols}


def from_pack(market: Any) -> dict[str, Any] | None:
    """The pack's own `market` block (`{"at", SYM: {"last", "change_pct"}}`) as the shown block, or None."""
    if not isinstance(market, Mapping):
        return None
    at = parse_iso(market.get("at"))
    symbols = _symbols({k: v for k, v in market.items() if k != "at"})
    if at is None or symbols is None:
        return None
    return {"source": "radar", "feed": None, "at": market.get("at"), "symbols": symbols}


def _fresh(block: Mapping[str, Any] | None, now: datetime, max_age_s: float) -> datetime | None:
    if block is None:
        return None
    at = parse_iso(block.get("at"))
    if at is None:
        return None
    age = (now - at).total_seconds()
    return at if -FUTURE_TOLERANCE_S <= age <= max_age_s else None


def shown_market(pack_market: Any, rec: Any, now: datetime, max_age_s: float,
                 order: Iterable[str] = ()) -> dict[str, Any] | None:
    """The block a read is shown: the newer of Scout's record and the pack's block, each only within
    `max_age_s` of `now`; a tie goes to Scout's record. None when neither qualifies."""
    best: tuple[datetime, dict[str, Any]] | None = None
    for block in (from_record(rec, order), from_pack(pack_market)):
        at = _fresh(block, now, max_age_s)
        if at is not None and block is not None and (best is None or at > best[0]):
            best = (at, block)
    return best[1] if best is not None else None


def market_line(block: Mapping[str, Any] | None, now: datetime, today: date | None = None) -> str:
    """The prompt's market line (see the module doc). `now` is the same moment as the "Now" line."""
    from scout.llm_prompts import clean_ticker  # noqa: PLC0415
    from scout.reads_prompt import _et, _pct, _to_places  # noqa: PLC0415 - reads_prompt imports this module

    if not block:
        return "Market: not available"
    at = parse_iso(block.get("at"))
    symbols = block.get("symbols") if isinstance(block.get("symbols"), Mapping) else {}
    parts = [f"{clean_ticker(s) or '?'} {_to_places(v.get('last'), 2)} {_pct(v.get('change_pct'))}"
             for s, v in list(symbols.items())[:6] if isinstance(v, Mapping)]
    if at is None or not parts:
        return "Market: not available"
    age = (now - at).total_seconds()
    when = _et(at, today)
    if age >= AGE_SHOWN_FROM_S:
        minutes = int(age // 60)
        when += f" ({minutes} minutes before now)"
    return f"Market at {when}: " + "; ".join(parts)
