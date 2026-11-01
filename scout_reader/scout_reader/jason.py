"""Jason tags for Radar's finds, from Scout's snapshot file (SCOUT_SPEC 5.5, 9.1; DESIGN 3.8, D10).

Stdlib only; nothing here raises into the caller. Every tag is descriptive: **no tag is ever a
gate**, and Jason's levels satisfy neither gate 5 nor gate 6.

`jason_tags_from(snapshot, symbol, price, as_of, near_pct, frame_prices)` returns four Results
(`{"value", "evidence", "stale", "reasons"}` plus extra keys):

- `on_jason_list`: a current setup names the symbol; adds `source_kind`, `direction` (the newest
  setup) and `setups` (every current one, newest first).
- `near_jason_level`: `price` is within `near_pct` (`jason_near_level_pct`, default 0.01) of a
  level reading of any current setup for the symbol. Every reading of an ambiguous level counts and
  the tag says so (`ambiguous`, `matches[].readings`).
- `jason_conflict`: the ported `conflict_for` rules in pure form over the newest setup; reason codes
  in `codes` (`jason_short`, `jason_avoid`, `below_jason_invalidation`, `below_jason_support`).
- `jason_market_frame`: Jason's setups for the market symbols (SPY, QQQ), preferring the newest
  current Tuesday read; above/below each level only for symbols whose price the caller passes in
  `frame_prices` (D10: Scout has no SPY/QQQ price).

As of (SS 2 rule 9): only snapshot lines with `first_seen_at <= as_of` count, the latest one
governs, and a setup counts only while `as_of`'s ET date is on or before its `expires`. No visible
snapshot means `unknown`. Freshness (the beat) is the caller's rule: pass `stale=True` and every
tag is `unknown` with reason `scout_stale`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scout_reader._et import utc_to_et
from scout_reader.records import JASON_SNAPSHOTS, UNKNOWN, known, parse_time, read_jsonl, result, visible

__all__ = [
    "DEFAULT_MARKET_SYMBOLS", "DEFAULT_NEAR_PCT", "TAGS", "current_for", "jason_tags_from", "jason_tags_in",
    "latest_snapshot",
]

TAGS = ("on_jason_list", "near_jason_level", "jason_conflict", "jason_market_frame")
DEFAULT_NEAR_PCT = 0.01
DEFAULT_MARKET_SYMBOLS = ("SPY", "QQQ")


def _all_unknown(reason: str, *, stale: bool = False) -> dict[str, dict[str, Any]]:
    return {tag: UNKNOWN(reason, stale=stale) for tag in TAGS}


def _dec(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    return out if out.is_finite() and out > 0 else None


def latest_snapshot(records: Iterable[Mapping[str, Any]], as_of: datetime) -> dict[str, Any] | None:
    """The snapshot line with the latest `first_seen_at <= as_of` (known format only), or None."""
    best: dict[str, Any] | None = None
    best_at: datetime | None = None
    for rec in records:
        if not isinstance(rec, Mapping) or not known(rec) or not visible(rec, as_of):
            continue
        at = parse_time(rec.get("first_seen_at"))
        if at is not None and (best_at is None or at >= best_at):
            best, best_at = dict(rec), at
    return best


def current_for(snapshot: Mapping[str, Any], symbol: str, today_iso: str) -> list[dict[str, Any]]:
    """The snapshot's setups for `symbol` current on `today_iso`, newest session first."""
    name = str(symbol or "").strip().upper()
    rows = [s for s in snapshot.get("setups") or () if isinstance(s, Mapping)
            and str(s.get("symbol", "")).upper() == name and today_iso <= str(s.get("expires") or "")]
    # the writer already orders newest first; a stable sort keeps that within one session date
    return sorted(rows, key=lambda s: str(s.get("session_date") or ""), reverse=True)


def _evidence(setup: Mapping[str, Any]) -> str:
    return f"jason:{setup.get('session_date')}.{setup.get('source_kind')}:{setup.get('symbol')}"


def _readings(level: Mapping[str, Any]) -> list[Decimal]:
    out = [_dec(r) for r in level.get("readings") or ()]
    return sorted({r for r in out if r is not None})


def _levels(setup: Mapping[str, Any], kind: str) -> list[Mapping[str, Any]]:
    return [lvl for lvl in setup.get("levels") or () if isinstance(lvl, Mapping) and lvl.get("kind") == kind]


# --------------------------------------------------------------------------- the four tags


def _on_list(setups: list[dict[str, Any]]) -> dict[str, Any]:
    if not setups:
        out = result("false", reasons=("not_on_jason_list",))
        out.update(source_kind=None, direction=None, setups=[])
        return out
    newest = setups[0]
    out = result("true", [_evidence(s) for s in setups])
    out.update(source_kind=newest.get("source_kind"), direction=newest.get("direction"),
               setups=[{k: s.get(k) for k in ("source_kind", "direction", "attribution", "session_date", "expires")}
                       for s in setups])
    return out


def _near(setups: list[dict[str, Any]], price: Decimal | None, near_pct: Decimal) -> dict[str, Any]:
    if not setups:
        out = result("false", reasons=("not_on_jason_list",))
        out.update(matches=[], ambiguous=False)
        return out
    if price is None:
        return UNKNOWN("no_price")
    matches: list[dict[str, Any]] = []
    for setup in setups:
        for lvl in setup.get("levels") or ():
            if not isinstance(lvl, Mapping):
                continue
            readings = _readings(lvl)
            for reading in readings:
                distance = abs(price - reading) / reading
                if distance <= near_pct:
                    matches.append({"kind": lvl.get("kind"), "reading": str(reading),
                                    "readings": [str(r) for r in readings], "ambiguous": bool(lvl.get("ambiguous")),
                                    "distance_pct": float(round(distance, 6)), "source_kind": setup.get("source_kind"),
                                    "session_date": setup.get("session_date")})
    ambiguous = any(m["ambiguous"] for m in matches)
    reasons = ["ambiguous_level: every reading counts"] if ambiguous else []
    out = result("true" if matches else "false", sorted({_evidence(s) for s in setups}), reasons=reasons)
    out.update(matches=matches, ambiguous=ambiguous, near_pct=float(near_pct))
    return out


def _conflict(setups: list[dict[str, Any]], price: Decimal | None) -> dict[str, Any]:
    if not setups:
        out = result("false", reasons=("not_on_jason_list",))
        out.update(codes=[], level=None)
        return out
    setup = setups[0]
    codes: list[str] = []
    direction = str(setup.get("direction") or "")
    if direction in ("short", "avoid"):
        codes.append(f"jason_{direction}")
    invalidations = [r for lvl in _levels(setup, "invalidation") for r in _readings(lvl)]
    supports = [r for lvl in _levels(setup, "support") for r in _readings(lvl)]
    breached: tuple[str, Decimal] | None = None
    if price is not None:
        if invalidations:
            highest = max(invalidations)
            if price < highest:
                breached = ("invalidation", highest)
                codes.append("below_jason_invalidation")
        elif supports:
            lowest = min(supports)
            if price < lowest:
                breached = ("support", lowest)
                codes.append("below_jason_support")
    elif not codes and (invalidations or supports):
        return UNKNOWN("no_price")
    out = result("true" if codes else "false", [_evidence(setup)], reasons=codes)
    level = {"kind": breached[0], "price": str(breached[1])} if breached else None
    out.update(codes=codes, level=level, direction=direction or None, source_kind=setup.get("source_kind"))
    return out


def _frame(snapshot: Mapping[str, Any], today_iso: str, market_symbols: Iterable[str],
           frame_prices: Mapping[str, Any] | None) -> dict[str, Any]:
    prices = {str(k).upper(): _dec(v) for k, v in (frame_prices or {}).items()}
    frames: dict[str, Any] = {}
    evidence: list[str] = []
    for sym in market_symbols:
        name = str(sym).upper()
        setups = current_for(snapshot, name, today_iso)
        if not setups:
            continue
        tuesday = [s for s in setups if s.get("source_kind") == "tuesday"]
        setup = (tuesday or setups)[0]
        evidence.append(_evidence(setup))
        price = prices.get(name)
        levels = []
        for lvl in setup.get("levels") or ():
            if not isinstance(lvl, Mapping):
                continue
            readings = _readings(lvl)
            entry: dict[str, Any] = {"kind": lvl.get("kind"), "readings": [str(r) for r in readings],
                                     "ambiguous": bool(lvl.get("ambiguous"))}
            if price is not None and readings:
                entry["position"] = ("above" if price > readings[-1] else "below" if price < readings[0]
                                     else "at" if len(readings) == 1 else "inside")
            levels.append(entry)
        frame: dict[str, Any] = {"source_kind": setup.get("source_kind"), "session_date": setup.get("session_date"),
                                 "expires": setup.get("expires"), "direction": setup.get("direction"),
                                 "price": str(price) if price is not None else None, "levels": levels}
        if price is not None:
            frame["above"] = sum(1 for lvl in levels if lvl.get("position") == "above")
            frame["below"] = sum(1 for lvl in levels if lvl.get("position") == "below")
        frames[name] = frame
    reasons = [] if frames else ["no_market_setups"]
    if frames and not any(f["price"] is not None for f in frames.values()):
        reasons.append("no_frame_prices: levels listed without above/below")
    out = result("true" if frames else "false", evidence, reasons=reasons)
    out["frames"] = frames
    return out


# --------------------------------------------------------------------------- entry points


def jason_tags_from(snapshot: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None, symbol: str,
                    price: float | str | Decimal | None, as_of: datetime, near_pct: float | None = None,
                    frame_prices: Mapping[str, Any] | None = None, *,
                    market_symbols: Iterable[str] | None = None, stale: bool = False) -> dict[str, dict[str, Any]]:
    """The four Jason tags for `symbol` at `price`, as of `as_of` (aware). `snapshot` is one
    snapshot record or the list of snapshot lines (the latest visible one is used). Never raises."""
    try:
        if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
            return _all_unknown("naive_as_of")
        if stale:
            return _all_unknown("scout_stale", stale=True)
        if snapshot is None:
            return _all_unknown("no_jason_snapshot")
        records = [snapshot] if isinstance(snapshot, Mapping) else list(snapshot)
        snap = latest_snapshot(records, as_of)
        if snap is None:
            return _all_unknown("no_jason_snapshot")
        today_iso = utc_to_et(as_of).date().isoformat()
        setups = current_for(snap, symbol, today_iso)
        pct = _dec(near_pct if near_pct is not None else DEFAULT_NEAR_PCT) or Decimal(str(DEFAULT_NEAR_PCT))
        parsed_price = _dec(price) if price is not None else None
        markets = tuple(market_symbols) if market_symbols is not None else DEFAULT_MARKET_SYMBOLS
        return {
            "on_jason_list": _on_list(setups),
            "near_jason_level": _near(setups, parsed_price, pct),
            "jason_conflict": _conflict(setups, parsed_price),
            "jason_market_frame": _frame(snap, today_iso, markets, frame_prices),
        }
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - the reader never raises into the caller
        return _all_unknown(f"error:{type(exc).__name__}")


def jason_tags_in(scout_dir: str | os.PathLike[str], symbol: str, price: float | str | Decimal | None,
                  as_of: datetime, near_pct: float | None = None, frame_prices: Mapping[str, Any] | None = None,
                  **kw: Any) -> dict[str, dict[str, Any]]:
    """`jason_tags_from` over `<scout_dir>\\state\\jason\\snapshots.jsonl`. Never raises."""
    try:
        records = read_jsonl(Path(scout_dir) / JASON_SNAPSHOTS)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001
        return _all_unknown(f"error:{type(exc).__name__}")
    return jason_tags_from(records, symbol, price, as_of, near_pct, frame_prices, **kw)
