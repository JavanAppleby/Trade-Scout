"""`market_context` over Scout's macro history (SCOUT_SPEC 7.3, 7.6; DESIGN 3.8). Pure, stdlib only,
never raises. Record only: nothing here gates a hold unless the answers unit applies
`params.hold_block_macro_events` (default false).

`market_context_from(history, facts, params, as_of, *, beat=...)` returns four Results:
- `macro_event_next_session`: over gate 5's window [as_of, next 09:30 ET open). `true` when any
  visible source (nowflation, BLS, the FOMC table in `params`, or a `macro_event` fact) puts a
  release in the window; `false` only when none does and nowflation is fresh, or, nowflation not
  fresh, the BLS file is fresh and the FOMC table reaches past the window (SCOUT_SPEC 7.3's
  fallback); `unknown` otherwise, and when the calendar does not cover the next session.
- `next_macro_event`: the earliest release at or after as_of (`event`); `false` when none is known
  and the sources are fresh as above.
- `inflation_regime`: `true` with `regime` (hot, cooling, steady) from the latest visible
  nowflation record when it is `ok` and still fresh; else `unknown` (`regime: "unknown"`).
- `vix`: `true` with `close` and `date` from the latest visible Cboe record when `ok` and fresh.

A record is fresh at as_of while `status == "ok"` and as_of < its `stale_after` (written by Scout
from its config). When `beat` is passed: None means Scout is stale (every field unknown), and a
source whose beat status is not `ok` is not fresh (DESIGN 3.2 rules 2 and 6).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time
from typing import Any

from scout_reader import records as R
from scout_reader._et import et_to_utc, utc_to_et
from scout_reader.calendar import next_open_after

FIELDS = ("macro_event_next_session", "inflation_regime", "vix", "next_macro_event")
SOURCES = ("nowflation", "bls", "fomc", "cboe_vix")
_UNSET: Any = object()

__all__ = ["FIELDS", "load_macro_history", "market_context_at", "market_context_from"]


def _aware(as_of: Any) -> bool:
    return isinstance(as_of, datetime) and as_of.tzinfo is not None and as_of.utcoffset() is not None


def _all_unknown(reason: str, *, stale: bool = False) -> dict[str, Any]:
    out = {name: R.UNKNOWN(reason, stale=stale) for name in FIELDS}
    out["inflation_regime"]["regime"] = "unknown"
    out["vix"].update(close=None, date=None)
    out["next_macro_event"]["event"] = None
    return out


def market_context_from(history: Iterable[Mapping[str, Any]], facts: Iterable[Mapping[str, Any]] | None,
                        params: Mapping[str, Any] | None, as_of: datetime, *, beat: Any = _UNSET) -> dict[str, Any]:
    try:
        return _context(history, facts, params, as_of, beat)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - the reader never raises into its caller
        return _all_unknown(f"error:{type(exc).__name__}")


def _latest(history: Iterable[Mapping[str, Any]], as_of: datetime) -> dict[str, Mapping[str, Any]]:
    best: dict[str, tuple[datetime, Mapping[str, Any]]] = {}
    for rec in history or ():
        if not isinstance(rec, Mapping) or not R.known(rec) or rec.get("source") not in SOURCES:
            continue
        if not R.visible(rec, as_of):
            continue
        at = R.parse_time(rec.get("first_seen_at"))
        if at is None:
            continue
        src = str(rec["source"])
        if src not in best or at >= best[src][0]:
            best[src] = (at, rec)
    return {k: v[1] for k, v in best.items()}


def _fresh(rec: Mapping[str, Any] | None, as_of: datetime, beat: Any, source: str) -> tuple[bool, str | None]:
    if rec is None:
        return False, f"no_record:{source}"
    if rec.get("status") != "ok":
        return False, f"not_ok:{source}"
    until = R.parse_time(rec.get("stale_after"))
    if until is None or as_of >= until:
        return False, f"stale:{source}"
    if beat is not _UNSET and isinstance(beat, Mapping):
        sources = beat.get("sources")
        if source in ("nowflation", "bls", "cboe_vix") and (
                not isinstance(sources, Mapping) or sources.get(source) != "ok"):
            return False, f"source_not_ok:{source}"
    return True, None


def _events(latest: Mapping[str, Mapping[str, Any]], facts: Iterable[Mapping[str, Any]] | None,
            params: Mapping[str, Any], as_of: datetime) -> list[tuple[datetime, str, str]]:
    """(event_at, kind, source) from the latest record per source, the params' FOMC table and the
    visible macro_event facts."""
    out: list[tuple[datetime, str, str]] = []
    for source in ("nowflation", "bls"):
        rec = latest.get(source)
        for ev in (rec or {}).get("events") or ():
            if isinstance(ev, Mapping):
                at = R.parse_time(ev.get("event_at"))
                if at is not None:
                    out.append((at, str(ev.get("kind") or "other"), source))
    for day in _fomc_dates(params):
        out.append((_fomc_at(day, params), "fomc", "fomc"))
    for rec in facts or ():
        if (isinstance(rec, Mapping) and R.known(rec) and rec.get("kind") == "macro_event"
                and R.visible(rec, as_of)):
            at = R.parse_time(rec.get("event_at"))
            if at is not None:
                out.append((at, str(rec.get("event_kind") or "other"), str(rec.get("source") or "fact")))
    return sorted(out)


def _fomc_dates(params: Mapping[str, Any]) -> list[date]:
    out: list[date] = []
    for text in params.get("fomc_dates") or ():
        try:
            out.append(date.fromisoformat(str(text)))
        except ValueError:
            continue
    return out


def _fomc_at(day: date, params: Mapping[str, Any]) -> datetime:
    try:
        hh, mm = str(params.get("fomc_decision_time_et") or "14:00").split(":")
        t = time(int(hh), int(mm))
    except ValueError:
        t = time(14, 0)
    return et_to_utc(datetime(day.year, day.month, day.day, t.hour, t.minute))


def _label(ev: tuple[datetime, str, str]) -> str:
    return f"{ev[1]}@{ev[0].strftime('%Y-%m-%dT%H:%M:%SZ')}({ev[2]})"


def _context(history: Iterable[Mapping[str, Any]], facts: Iterable[Mapping[str, Any]] | None,
             params: Mapping[str, Any] | None, as_of: datetime, beat: Any) -> dict[str, Any]:
    if not _aware(as_of):
        return _all_unknown("naive_as_of")
    if beat is None:
        return _all_unknown("scout_stale", stale=True)
    if not isinstance(params, Mapping):
        return _all_unknown("no_params")
    latest = _latest(history, as_of)
    now_ok, now_why = _fresh(latest.get("nowflation"), as_of, beat, "nowflation")
    bls_ok, bls_why = _fresh(latest.get("bls"), as_of, beat, "bls")
    vix_ok, vix_why = _fresh(latest.get("cboe_vix"), as_of, beat, "cboe_vix")
    events = _events(latest, facts, params, as_of)
    window_end = next_open_after(as_of)
    fomc = _fomc_dates(params)
    out: dict[str, Any] = {}

    def covered(until: datetime | None) -> tuple[bool, list[str]]:
        if now_ok:
            return True, []
        reasons = [r for r in (now_why,) if r]
        if bls_ok and fomc and until is not None and _fomc_at(max(fomc), params) >= until:
            return True, [*reasons, "fallback:bls+fomc"]
        return False, [*reasons, *(r for r in (bls_why,) if r),
                       *([] if fomc and until is not None and _fomc_at(max(fomc), params) >= until
                         else ["fomc_table_short"])]

    if window_end is None:
        out["macro_event_next_session"] = R.UNKNOWN("calendar_not_covered")
    else:
        inside = [e for e in events if as_of <= e[0] < window_end]
        if inside:
            out["macro_event_next_session"] = R.result("true", evidence=[_label(e) for e in inside])
        else:
            ok, reasons = covered(window_end)
            out["macro_event_next_session"] = R.result("false" if ok else "unknown", reasons=reasons)

    upcoming = [e for e in events if e[0] >= as_of]
    if upcoming:
        first = upcoming[0]
        res = R.result("true", evidence=[_label(first)])
        res["event"] = {"kind": first[1], "event_at": first[0].strftime("%Y-%m-%dT%H:%M:%SZ"), "source": first[2],
                        "event_at_et": utc_to_et(first[0]).strftime("%Y-%m-%d %H:%M")}
    else:
        ok, reasons = covered(window_end)
        res = R.result("false" if ok else "unknown", reasons=reasons)
        res["event"] = None
    out["next_macro_event"] = res

    nf = latest.get("nowflation")
    regime = (nf or {}).get("inflation_regime")
    if now_ok and regime in ("hot", "cooling", "steady"):
        res = R.result("true", evidence=[f"nowflation@{nf.get('first_seen_at')}"])
        res["regime"] = regime
        res["attribution"] = (nf or {}).get("attribution")
    else:
        res = R.result("unknown", reasons=[now_why or "regime_unknown"])
        res["regime"] = "unknown"
    out["inflation_regime"] = res

    vrec = latest.get("cboe_vix")
    vix = (vrec or {}).get("vix")
    if vix_ok and isinstance(vix, Mapping) and isinstance(vix.get("close"), (int, float)):
        res = R.result("true", evidence=[f"cboe_vix@{vrec.get('first_seen_at')}"])
        res.update(close=vix.get("close"), date=vix.get("date"))
    else:
        res = R.result("unknown", reasons=[vix_why or "no_vix"])
        res.update(close=None, date=None)
    out["vix"] = res
    return out


def load_macro_history(scout_dir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    return R.read_jsonl(os.path.join(os.fspath(scout_dir), R.MACRO_HISTORY))


def market_context_at(scout_dir: str | os.PathLike[str], as_of: datetime, stale_s: float | None = None,
                      facts: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Convenience for `scout_reader.api`: reads params, the governing beat and the history, then
    `market_context_from`. Never raises."""
    try:
        if not _aware(as_of):
            return _all_unknown("naive_as_of")
        params = R.last_params(scout_dir, as_of)
        if stale_s is None:
            value = (params or {}).get("scout_stale_s")
            stale_s = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else R.DEFAULT_STALE_S
        beat = R.governing_beat(scout_dir, as_of, stale_s)
        return market_context_from(load_macro_history(scout_dir), facts, params, as_of, beat=beat)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001
        return _all_unknown(f"error:{type(exc).__name__}")
