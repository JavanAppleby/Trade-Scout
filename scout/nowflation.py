"""nowflation.com: release calendar, realtime nowcast, forecasts and the `.ics` fallback
(SCOUT_SPEC 4.1 #7, 7.3; DESIGN 2.3).

Pinned schemas: a body that does not match raises `SchemaError` and Scout refuses the source
(status `unavailable`) rather than skipping rows. Extra keys are allowed (keys may be added). The
`realtime.json` and `forecasts/index.json` schemas follow the live files read on 2026-10-31
(DESIGN 2.3 "nowflation's live layout"). Times
come from `datetime_utc`; the ET text is never parsed. Optional per-event values (`prior`, `street`,
`nowflation_forecast`, `cleveland`, `kalshi`) are stored when present and never assumed.

`freshness` is SCOUT_SPEC 7.3's fail-closed list. `NowflationReader.read` makes at most
min(`host_max_requests_per_day["nowflation.com"]`, 10) requests per ET day, counting the requests the
fetcher has already sent today (its count survives a restart; the fetcher enforces the cap too), and
reads `/forecasts/index.json` at most once a day (it is informational only): after a restart, a copy
saved under today's raw folder is read back instead of asking again.

Credit: "Nowflation" with a link (https://nowflation.com) wherever its data appears; raw files are
kept for replay and never republished.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from scout.bls_ics import IcsEvent, _board, _health
from scout.bls_ics import parse_ics as _parse_ics
from scout.clock import Clock, et_date, et_datetime, iso, parse_iso
from scout.config import ScoutConfig
from scout.http_state import hard_cap
from scout.macro import (ATTRIBUTION, VALUE_KEYS, MacroEvent, SchemaError, forecast_lean, inflation_regime,
                         normalise_kind)
from scout.models import FetchResult, MacroRecord
from scout.store import sha256_text

log = logging.getLogger("scout.nowflation")

HOST = "nowflation.com"
DEFAULT_DAILY_CAP = 10
FUTURE_SLACK = timedelta(hours=1)  # a generated_at this far ahead of Scout's clock is drift
CAL_KEYS = ("generated_at", "from", "source", "events", "past", "fomc")
REALTIME_KEYS = ("realtime_yoy_pct", "cpi_gauge_yoy_pct", "official_cpi_yoy_pct", "official_cpi_month",
                 "live_share_pct", "avg_input_age_days")
_SKIP_EXTRA = frozenset({"kind", "datetime_utc", "datetime_et", "date_et", "time_et", *VALUE_KEYS})

__all__ = [
    "NowCalendar", "NowForecasts", "NowRealtime", "NowflationReader", "freshness", "is_release_day", "parse_forecasts",
    "ics_events", "ics_kind", "parse_ics", "parse_realtime", "parse_release_calendar",
]


@dataclass(frozen=True)
class NowCalendar:
    generated_at: datetime
    from_: str
    source: Any
    events: tuple[MacroEvent, ...]
    past_count: int
    fomc: tuple[MacroEvent, ...]


@dataclass(frozen=True)
class NowRealtime:
    generated_at: datetime | None
    run_ok: bool
    checks_ok: bool
    record_type: str
    last_good_held: bool
    stale_weight_pct: float | None
    values: dict[str, Any] = field(default_factory=dict)  # REALTIME_KEYS
    changes: dict[int, float] = field(default_factory=dict)  # window days -> change in pp

    def to_record(self) -> dict[str, Any]:
        out = {k: self.values.get(k) for k in REALTIME_KEYS}
        for n in (1, 5, 20):
            out[f"change_{n}d"] = self.changes.get(n)
        return {**{k: out[k] for k in ("realtime_yoy_pct", "change_1d", "change_5d", "change_20d")},
                **{k: out[k] for k in REALTIME_KEYS[1:]}}


@dataclass(frozen=True)
class NowForecasts:
    generated_at: datetime | None
    items: tuple[dict[str, Any], ...]  # one per receipt (print month), newest first as published
    graded: int  # `n_graded`
    latest: str | None = None  # the latest print month

    def receipt(self, print_month: Any) -> dict[str, Any] | None:
        return next((r for r in self.items if r.get("print_month") == print_month), None)


# --------------------------------------------------------------------------- parsing helpers


def _load(body: bytes, what: str) -> dict[str, Any]:
    if not isinstance(body, (bytes, bytearray)):
        raise SchemaError(f"{what}: body is not bytes")
    try:
        obj = json.loads(bytes(body).decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SchemaError(f"{what}: not JSON") from exc
    if not isinstance(obj, dict):
        raise SchemaError(f"{what}: top level is not an object")
    return obj


def _req(obj: Mapping[str, Any], key: str, types: type | tuple[type, ...], where: str) -> Any:
    if key not in obj:
        raise SchemaError(f"{where}: missing {key!r}")
    value = obj[key]
    if isinstance(value, bool) and bool not in (types if isinstance(types, tuple) else (types,)):
        raise SchemaError(f"{where}: {key!r} has the wrong type")
    if not isinstance(value, types):
        raise SchemaError(f"{where}: {key!r} has the wrong type")
    return value


def _utc(value: Any, where: str) -> datetime:
    at = parse_iso(value)
    if at is None:
        raise SchemaError(f"{where}: not an ISO UTC time")
    return at


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _event(item: Any, where: str) -> MacroEvent:
    if not isinstance(item, dict):
        raise SchemaError(f"{where}: not an object")
    kind = _req(item, "kind", str, where)
    if not kind.strip():
        raise SchemaError(f"{where}: empty 'kind'")
    at = _utc(_req(item, "datetime_utc", str, where), f"{where}.datetime_utc")
    extra = {k: v for k, v in item.items() if k not in _SKIP_EXTRA and isinstance(v, (str, int, float, bool))}
    title = item.get("title") or item.get("name")
    return MacroEvent(kind=normalise_kind(kind), event_at=at, source="nowflation",
                      title=title if isinstance(title, str) else None, extra=extra,
                      **{k: item.get(k) for k in VALUE_KEYS})


def _fomc_item(item: Any, where: str, cfg_time: Any | None) -> MacroEvent:
    if isinstance(item, str):
        item = {"date": item}
    if not isinstance(item, dict):
        raise SchemaError(f"{where}: not an object or date")
    if "datetime_utc" in item:
        at = _utc(item["datetime_utc"], f"{where}.datetime_utc")
    elif isinstance(item.get("date"), str):
        try:
            day = date.fromisoformat(item["date"])
        except ValueError as exc:
            raise SchemaError(f"{where}.date: not YYYY-MM-DD") from exc
        at = et_datetime(day, cfg_time) if cfg_time is not None else datetime(day.year, day.month, day.day,
                                                                              tzinfo=timezone.utc)
    else:
        raise SchemaError(f"{where}: neither 'datetime_utc' nor 'date'")
    return MacroEvent(kind="fomc", event_at=at, source="nowflation", title="FOMC decision",
                      extra={"from_fomc_list": True})


# --------------------------------------------------------------------------- pinned schemas


def parse_release_calendar(body: bytes, *, fomc_time: Any | None = None) -> NowCalendar:
    """`/data/release_calendar.json`: top level `generated_at, from, source, events, past, fomc`;
    each event an object with `kind` and `datetime_utc`. A date-only FOMC entry takes `fomc_time`
    (ET; `fomc_decision_time_et`)."""
    obj = _load(body, "release_calendar")
    for key in CAL_KEYS:
        if key not in obj:
            raise SchemaError(f"release_calendar: missing {key!r}")
    gen = _utc(_req(obj, "generated_at", str, "release_calendar"), "release_calendar.generated_at")
    frm = _req(obj, "from", str, "release_calendar")
    events = tuple(_event(e, f"events[{i}]") for i, e in enumerate(_req(obj, "events", list, "release_calendar")))
    past = _req(obj, "past", list, "release_calendar")
    for i, p in enumerate(past):
        if not isinstance(p, dict):
            raise SchemaError(f"past[{i}]: not an object")
    fomc = tuple(_fomc_item(f, f"fomc[{i}]", fomc_time)
                 for i, f in enumerate(_req(obj, "fomc", list, "release_calendar")))
    return NowCalendar(generated_at=gen, from_=frm, source=obj.get("source"), events=events, past_count=len(past),
                       fomc=fomc)


def parse_realtime(body: bytes) -> NowRealtime:
    """`/data/realtime.json`, pinned to the live layout read 2026-10-31. Required (a
    missing key or a wrong type refuses the source): `generated_at`, `run {ok, checks_ok}`,
    `today {record_type, last_good_held, realtime_yoy_pct}`, `freshness.realtime {live_share_pct}`.
    Optional: `freshness.realtime.stale_weight_pct` (fallback: the sum of
    `freshness.realtime.stale[].weight_pct`; neither -> None, which `freshness` treats as stale),
    `avg_input_age_days`, `today.cpi_gauge_yoy_pct`, `official_cpi_yoy_pct`, `official_cpi_month`.
    Changes: 1 day = `today.realtime_dod_pp`, 5 = `today.realtime_wow_pp` (nowflation's week: 7
    calendar days, 5 trading days), 20 = from `history` (see `_change_from_history`)."""
    obj = _load(body, "realtime")
    gen = _utc(_req(obj, "generated_at", str, "realtime"), "realtime.generated_at")
    run = _req(obj, "run", dict, "realtime")
    run_ok = _req(run, "ok", bool, "realtime.run")
    checks_ok = _req(run, "checks_ok", bool, "realtime.run")
    today = _req(obj, "today", dict, "realtime")
    record_type = _req(today, "record_type", str, "realtime.today")
    held = _req(today, "last_good_held", bool, "realtime.today")
    yoy = _req(today, "realtime_yoy_pct", (int, float), "realtime.today")
    fresh = _req(_req(obj, "freshness", dict, "realtime"), "realtime", dict, "realtime.freshness")
    live = _req(fresh, "live_share_pct", (int, float), "realtime.freshness.realtime")
    if _num(yoy) is None or _num(live) is None:
        raise SchemaError("realtime: realtime_yoy_pct or live_share_pct is not a finite number")
    stale_w = _num(fresh.get("stale_weight_pct"))
    if stale_w is None:
        stale_w = _stale_weight_from_list(fresh.get("stale"))
    changes: dict[int, float] = {}
    for n, key in ((1, "realtime_dod_pp"), (5, "realtime_wow_pp")):
        if _num(today.get(key)) is not None:
            changes[n] = float(today[key])
    c20 = _change_from_history(obj, today)
    if c20 is not None:
        changes[20] = c20
    month = today.get("official_cpi_month")
    values = {
        "realtime_yoy_pct": float(yoy), "cpi_gauge_yoy_pct": _num(today.get("cpi_gauge_yoy_pct")),
        "official_cpi_yoy_pct": _num(today.get("official_cpi_yoy_pct")),
        "official_cpi_month": month if isinstance(month, str) else None,
        "live_share_pct": float(live), "avg_input_age_days": _num(fresh.get("avg_input_age_days")),
    }
    return NowRealtime(generated_at=gen, run_ok=run_ok, checks_ok=checks_ok, record_type=record_type,
                       last_good_held=held, stale_weight_pct=stale_w, values=values, changes=changes)


def _stale_weight_from_list(stale: Any) -> float | None:
    """The documented fallback for `stale_weight_pct`: the sum of `stale[].weight_pct` (an empty
    list is 0). Anything unreadable gives None (stale)."""
    if not isinstance(stale, list):
        return None
    total = 0.0
    for item in stale:
        w = _num(item.get("weight_pct")) if isinstance(item, dict) else None
        if w is None:
            return None
        total += w
    return round(total, 6)


HISTORY_20D_DAYS = 28  # 20 trading days ~ 28 calendar days
HISTORY_20D_SLACK = 2  # a history point 26..30 calendar days back will do (the nearest to 28 wins)


def _change_from_history(obj: Mapping[str, Any], today: Mapping[str, Any]) -> float | None:
    """The 20-trading-day change: today's history point (else `today.realtime_yoy_pct`) minus the
    `history` point nearest 28 calendar days back (within 26..30). None when there is no such
    point, or it is not `record_type` "live", or its `method_version` differs from the file's
    (a change across a method switch is not like for like). `history` is optional."""
    hist, method = obj.get("history"), obj.get("method_version")
    try:
        day = date.fromisoformat(str(today.get("date")))
    except ValueError:
        return None
    if not isinstance(hist, list) or not isinstance(method, str):
        return None
    points: dict[date, Mapping[str, Any]] = {}
    for p in hist:
        if isinstance(p, dict) and isinstance(p.get("date"), str) and _num(p.get("realtime")) is not None:
            try:
                points[date.fromisoformat(p["date"])] = p
            except ValueError:
                continue
    base = _num(points[day]["realtime"]) if day in points else _num(today.get("realtime_yoy_pct"))
    for k in sorted(range(HISTORY_20D_DAYS - HISTORY_20D_SLACK, HISTORY_20D_DAYS + HISTORY_20D_SLACK + 1),
                    key=lambda k: (abs(k - HISTORY_20D_DAYS), k)):
        p = points.get(day - timedelta(days=k))
        if p is None:
            continue
        if p.get("record_type") != "live" or p.get("method_version") != method or base is None:
            return None
        return round(base - float(p["realtime"]), 6)
    return None


_HEADLINE = {"nowflation_forecast_ensemble": "forecast", "nowflation_nowcast_inhouse": "nowcast",
             "cleveland_fed": "cleveland", "kalshi_implied": "kalshi", "street_consensus": "street"}
_RECEIPT_KEYS = ("print_month", "status", "record_type", "pre_registered", "release_datetime_utc",
                 "official_forecast", "graded", "actual", "signed_error")


def parse_forecasts(body: bytes) -> NowForecasts:
    """`/forecasts/index.json`, pinned to the live layout read 2026-10-31. Required: `generated_at`,
    `n_graded` (int), `receipts` (a list of objects, each with a `print_month` string). Each receipt
    keeps `_RECEIPT_KEYS` and its `headline_yoy` values (forecast, nowcast, cleveland, kalshi,
    street) when present; `latest` and `horizons` are optional. Informational only (SS 7.3)."""
    obj = _load(body, "forecasts")
    gen = _utc(_req(obj, "generated_at", str, "forecasts"), "forecasts.generated_at")
    graded = _req(obj, "n_graded", int, "forecasts")
    receipts = _req(obj, "receipts", list, "forecasts")
    out: list[dict[str, Any]] = []
    for i, r in enumerate(receipts):
        if not isinstance(r, dict) or not isinstance(r.get("print_month"), str):
            raise SchemaError(f"receipts[{i}]: not an object with 'print_month'")
        item = {k: r.get(k) for k in _RECEIPT_KEYS if isinstance(r.get(k), (str, int, float, bool))}
        head = r.get("headline_yoy")
        if isinstance(head, dict):
            item.update({name: _num(head.get(key)) for key, name in _HEADLINE.items()})
        out.append(item)
    latest = obj.get("latest") if isinstance(obj.get("latest"), str) else None
    return NowForecasts(generated_at=gen, items=tuple(out), graded=graded, latest=latest)


def parse_ics(body: bytes) -> list[IcsEvent]:
    """`/nowflation.ics` (fallback when the JSON calendar fails); the shared lenient parser."""
    return _parse_ics(body)


# Full release names only for NFP: "employment", "jobs" or "payroll" alone would take in the ADP
# National Employment Report, the Employment Cost Index or JOLTS, and the merge would then pair that
# event with the real BLS NFP and move it to the wrong day.
_NFP_NAMES = ("employment situation", "nonfarm payroll", "non-farm payroll", "nfp")
_NOT_NFP = ("adp", "employment cost", "jolts", "job openings")


def ics_kind(summary: str) -> str | None:
    """The release kind of an `.ics` summary, or None for any other event."""
    s = " ".join(summary.lower().split())
    if "fomc" in s:
        return "fomc"
    if "pce" in s:
        return "pce"
    if "cpi" in s or "consumer price index" in s:
        return "cpi"
    if any(w in s for w in _NFP_NAMES) and not any(w in s for w in _NOT_NFP):
        return "nfp"
    return None


def ics_events(events: list[IcsEvent]) -> list[MacroEvent]:
    out: list[MacroEvent] = []
    for ev in events:
        if ev.all_day:
            continue
        kind = ics_kind(ev.summary)
        if kind is not None:
            out.append(MacroEvent(kind=kind, event_at=ev.start, source="nowflation", title=ev.summary,
                                  extra={"from_ics": True}))
    return sorted(out, key=lambda e: e.event_at)


# --------------------------------------------------------------------------- freshness


def freshness(cal: NowCalendar | None, rt: NowRealtime | None, now: datetime, cfg: ScoutConfig) -> tuple[str, list[str]]:
    """("ok" | "stale" | "unavailable", reasons). SCOUT_SPEC 7.3: unavailable without both files;
    stale when `generated_at` is older than `nowflation_max_age_h` or, after
    `nowflation_fresh_after_et`, not today's ET date; `run.ok` or `run.checks_ok` false;
    `today.record_type` not "live"; `last_good_held` true; the stale share of the market-sourced
    weight above `nowflation_max_stale_weight_pct` (or not given; see `_stale_weight_reasons`)."""
    reasons: list[str] = []
    if cal is None:
        reasons.append("no_release_calendar")
    if rt is None:
        reasons.append("no_realtime")
    if reasons:
        return "unavailable", reasons
    assert cal is not None and rt is not None
    today = et_date(now)
    after = now >= et_datetime(today, cfg.nowflation_fresh_after_et)
    for name, gen in (("calendar", cal.generated_at), ("realtime", rt.generated_at)):
        if gen is None:
            continue
        if gen > now + FUTURE_SLACK:
            reasons.append(f"{name}_generated_in_future")
        if now - gen > timedelta(hours=cfg.nowflation_max_age_h):
            reasons.append(f"{name}_generated_at_old")
        if after and et_date(gen) != today:
            reasons.append(f"{name}_generated_at_not_today")
    if not rt.run_ok:
        reasons.append("run_not_ok")
    if not rt.checks_ok:
        reasons.append("checks_not_ok")
    if rt.record_type != "live":
        reasons.append(f"record_type:{rt.record_type[:20]}")
    if rt.last_good_held:
        reasons.append("last_good_held")
    reasons.extend(_stale_weight_reasons(rt, cfg.nowflation_max_stale_weight_pct))
    return ("stale" if reasons else "ok"), reasons


def _stale_weight_reasons(rt: NowRealtime, limit_pct: float) -> list[str]:
    """The SS 7.3 stale-weight gate on the live layout. nowflation's `stale_weight_pct` counts only
    market-sourced components whose sources went stale, and `live_share_pct` only the fresh ones;
    BLS-held components are neither. So the gate is the stale share of the market-sourced weight,
    `stale / (live + stale) * 100`, against `nowflation_max_stale_weight_pct`. No market-sourced
    weight at all (a nowcast of held BLS data only) is stale too."""
    if rt.stale_weight_pct is None:
        return ["stale_weight_missing"]
    live = _num(rt.values.get("live_share_pct"))
    if live is None:
        return ["live_share_missing"]
    market = live + rt.stale_weight_pct
    if market <= 0:
        return ["no_live_inputs"]
    share = 100.0 * rt.stale_weight_pct / market
    return [f"stale_weight:{share:.1f}"] if share > limit_pct else []


def is_release_day(events: Any, day: date) -> bool:
    """True when a CPI, NFP or PCE release falls on ET `day`."""
    return any(e.kind in ("cpi", "nfp", "pce") and et_date(e.event_at) == day for e in events or ())


# --------------------------------------------------------------------------- reader


class NowflationReader:
    """Reads nowflation's files into one MacroRecord per read (source "nowflation")."""

    def __init__(self, cfg: ScoutConfig, fetcher: Any, raw: Any, health: Any, board: Any, clock: Clock) -> None:
        self.cfg, self.fetcher, self.raw, self.health, self.board, self.clock = cfg, fetcher, raw, health, board, clock
        self.src = cfg.source("nowflation")
        self.cap = min(int(cfg.host_max_requests_per_day.get(HOST, DEFAULT_DAILY_CAP)), hard_cap(HOST) or 0)
        self.day: date | None = None
        self.used = 0
        self.forecasts: NowForecasts | None = None
        self.forecasts_day: date | None = None
        self.last_events: tuple[MacroEvent, ...] = ()
        self.last_ok: str | None = None
        self.last_read_at: datetime | None = None

    def url(self, suffix: str) -> str | None:
        for p in self.src.paths:
            if p.lower().endswith(suffix):
                return self.src.url.rstrip("/") + "/" + p.lstrip("/")
        return None

    def requests_left(self, now: datetime) -> int:
        """Today's budget left: the cap less the larger of this reader's count and the fetcher's
        count for the host (the fetcher's survives a restart)."""
        if self.day != et_date(now):
            self.day, self.used = et_date(now), 0
        sent = self.used
        counter = getattr(self.fetcher, "requests_today", None)
        if callable(counter):
            try:
                sent = max(sent, int(counter(HOST, now)))
            except Exception:  # noqa: BLE001 - a fake or odd fetcher: our own count stands
                pass
        return max(0, self.cap - sent)

    def _saved_forecasts(self, now: datetime) -> None:
        """After a restart: today's forecasts file from the raw folder, so it is not asked again."""
        if self.forecasts_day == et_date(now):
            return
        folder_of = getattr(self.raw, "folder", None)
        if not callable(folder_of):
            return
        try:
            saved = sorted(folder_of("nowflation", now).glob("*forecasts_index.json"))
            if saved:
                self.forecasts, self.forecasts_day = parse_forecasts(saved[-1].read_bytes()), et_date(now)
        except Exception:  # noqa: BLE001 - unreadable copy: ask again (within the cap)
            return

    def _fetch(self, url: str) -> FetchResult:
        self.used += 1
        return self.fetcher.fetch("nowflation", url, conditional=False)

    def _get(self, now: datetime, suffix: str, feed: str, parse: Any) -> tuple[Any, str | None, list[str]]:
        """(parsed or None, sha256 or None, reasons)."""
        url = self.url(suffix)
        if url is None:
            return None, None, [f"{feed}:not_configured"]
        got = self._fetch(url)
        if not got.ok or got.body is None:
            _health(self.health, self.cfg, "nowflation", feed, got, got.result, 0, got.error)
            return None, None, [f"{feed}:fetch:{got.result}"]
        sha = self.raw.save("nowflation", feed, got.body, now)
        try:
            parsed = parse(got.body)
        except SchemaError as exc:
            _health(self.health, self.cfg, "nowflation", feed, got, "schema", 0, str(exc)[:200])
            return None, sha, [f"{feed}:schema"]
        if isinstance(parsed, list):
            n = len(parsed)
        else:
            n = len(getattr(parsed, "events", None) or getattr(parsed, "items", None) or ()) or 1
        _health(self.health, self.cfg, "nowflation", feed, got, "ok", n, None)
        return parsed, sha, []

    def read(self, now: datetime) -> MacroRecord | None:
        """One read: calendar and realtime (2 requests), the `.ics` when the calendar fails, the
        forecasts once a day. None when today's request budget cannot cover the read."""
        left = self.requests_left(now)
        self.last_read_at = now
        self._saved_forecasts(now)
        if left < 2:
            log.info("nowflation: daily request budget used (%d of %d); read skipped", self.used, self.cap)
            return None
        fomc_time = self.cfg.fomc_decision_time_et
        cal, cal_sha, reasons = self._get(now, "release_calendar.json", "release_calendar.json",
                                          lambda b: parse_release_calendar(b, fomc_time=fomc_time))
        rt, rt_sha, r2 = self._get(now, "realtime.json", "realtime.json", parse_realtime)
        reasons += r2
        events: tuple[MacroEvent, ...] = ()
        if cal is not None:
            events = tuple(sorted((*cal.events, *cal.fomc), key=lambda e: e.event_at))
        elif self.requests_left(now) >= 1:
            ics, _sha, r3 = self._get(now, ".ics", "nowflation.ics", parse_ics)
            reasons += r3
            if ics is not None:
                events = tuple(ics_events(ics))
                reasons.append("calendar_from_ics")
        if self.forecasts_day != et_date(now) and self.requests_left(now) >= 1 and self.url("index.json"):
            fc, _sha, r4 = self._get(now, "index.json", "forecasts_index.json", parse_forecasts)
            if fc is not None:
                self.forecasts, self.forecasts_day = fc, et_date(now)
            else:
                reasons += [f"informational:{r}" for r in r4]
        status, fresh_reasons = freshness(cal, rt, now, self.cfg)
        if cal is not None and rt is not None:
            reasons += fresh_reasons
        elif not reasons:
            reasons += fresh_reasons
        regime = inflation_regime(rt, self.cfg) if status == "ok" else "unknown"
        if events:
            self.last_events = events
        if status == "ok":
            self.last_ok = iso(now)
        _board(self.board, self.cfg, "nowflation", status, self.last_ok, reasons[0] if reasons else None)
        sha = sha256_text(f"{cal_sha or ''}|{rt_sha or ''}") if (cal_sha or rt_sha) else None
        # the older of the two files' generated_at: SS 7.3 makes nowflation stale when either is not
        # today's after 09:15 ET, and macro.stale_after works from this one value
        gens = [g for g in (cal.generated_at if cal is not None else None,
                            rt.generated_at if rt is not None else None) if g is not None]
        gen = min(gens) if gens else None
        return MacroRecord(source="nowflation", first_seen_at=iso(now), fetched_at=iso(self.clock.now()),
                           status=status, reasons=tuple(reasons), source_generated_at=iso(gen) if gen else None,
                           sha256=sha, events=tuple(e.to_dict() for e in events),
                           realtime=rt.to_record() if rt is not None else None, inflation_regime=regime,
                           forecast_lean=tuple(forecast_lean(cal, self.forecasts)) if cal is not None else (),
                           vix=None, attribution=ATTRIBUTION)
