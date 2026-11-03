"""Macro events, the inflation regime and the macro files (SCOUT_SPEC 4.1 #7-10, 7.3, 7.6; DESIGN 2.3, 5.8).

Record and report only: nothing here gates a hold (`hold_block_macro_events` is the answers unit's).

- `MacroEvent`: one scheduled release (CPI, NFP, PCE, FOMC, ...) from one source, or a merged one.
- `merge_events`: CPI and NFP are cross-checked with the BLS `.ics` (same ET date, times within
  `macro_agree_minutes`); FOMC with the config table (same ET date); PCE has no second source, so
  `sources_agree = None`. When sources disagree the earlier time is used.
- `inflation_regime`: hot / cooling / steady / unknown from nowflation's realtime change over
  `inflation_regime_window_days` against `inflation_hot_pp` / `inflation_cooling_pp`.
- `forecast_lean`: forecast minus prior and minus street (from the calendar, filled from the
  forecasts receipt for the print month), labelled "informational, untested".
- `MacroWriter`: appends `state/macro/history.jsonl`, rewrites `state/macro/<ET date>.json` and adds
  `macro_event` facts for new events. Each history line also carries `stale_after` (an additive key,
  DESIGN 5.8 + this unit): the instant after which the reader stops treating the record as fresh.

Nowflation's data is credited "Nowflation" with a link wherever it appears; raw files are never
republished.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from scout_reader.calendar import calendar_covers, is_trading_day, next_trading_day

from scout import FORMAT
from scout.clock import Clock, et_date, et_datetime, iso, parse_iso
from scout.config import ScoutConfig
from scout.models import Fact, MacroRecord
from scout.paths import ScoutPaths
from scout.store import append_jsonl, atomic_write_json, read_jsonl

log = logging.getLogger("scout.macro")

ATTRIBUTION = "Nowflation https://nowflation.com"
LEAN_NOTE = "informational, untested"
MACRO_SOURCES = ("nowflation", "bls", "fomc", "cboe_vix", "bls_data")
REGIMES = ("hot", "cooling", "steady", "unknown")
VALUE_KEYS = ("prior", "street", "nowflation_forecast", "cleveland", "kalshi")
CROSS_CHECKED = {"cpi": "bls", "nfp": "bls", "fomc": "fomc"}  # kind -> second source
PAIR_WINDOW = timedelta(days=7)  # a same-kind event this close is the same release, disagreeing
_PRIMARY = {"nowflation": 0, "bls": 1, "fomc": 2}

__all__ = [
    "ATTRIBUTION", "LEAN_NOTE", "MACRO_SOURCES", "REGIMES", "MacroEvent", "MacroWriter", "SchemaError",
    "daily_as_of", "daily_body", "events_from_dicts", "fact_id", "forecast_lean", "inflation_regime",
    "merge_events", "merged_from", "normalise_kind",
    "stale_after",
]


class SchemaError(ValueError):
    """A source's body does not match its pinned schema: the source is refused (status
    `unavailable`), never read with rows skipped (SCOUT_SPEC 7.3, 14)."""


_KIND_ALIASES = {
    "cpi": "cpi", "consumer price index": "cpi",
    "nfp": "nfp", "jobs": "nfp", "payrolls": "nfp", "employment situation": "nfp", "employment": "nfp",
    "pce": "pce", "personal income and outlays": "pce",
    "fomc": "fomc", "fed": "fomc",
}


def normalise_kind(kind: object) -> str:
    """Lower-case release kind with the aliases above folded ("CPI" -> "cpi", "Jobs" -> "nfp")."""
    text = str(kind or "").strip().lower()
    return _KIND_ALIASES.get(text, text or "other")


@dataclass(frozen=True)
class MacroEvent:
    """One scheduled release. `event_at` is aware UTC. A merged event lists its `sources`."""

    kind: str
    event_at: datetime
    source: str
    title: str | None = None
    prior: Any = None
    street: Any = None
    nowflation_forecast: Any = None
    cleveland: Any = None
    kalshi: Any = None
    extra: dict[str, Any] = field(default_factory=dict)
    sources: tuple[str, ...] = ()
    sources_agree: bool | None = None
    times: dict[str, str] = field(default_factory=dict)  # source -> its event_at, when merged

    def to_dict(self) -> dict[str, Any]:
        """The history-record event shape (DESIGN 5.8); the title rides in `extra`."""
        extra = dict(self.extra)
        if self.title is not None:
            extra.setdefault("title", self.title)
        out: dict[str, Any] = {"kind": self.kind, "event_at": iso(self.event_at)}
        for key in VALUE_KEYS:
            out[key] = getattr(self, key)
        out["extra"] = extra
        return out

    def to_merged_dict(self) -> dict[str, Any]:
        out = self.to_dict()
        out["sources"] = list(self.sources or (self.source,))
        out["sources_agree"] = self.sources_agree
        out["times"] = dict(self.times)
        return out


def events_from_dicts(items: Iterable[Mapping[str, Any]], source: str) -> list[MacroEvent]:
    """MacroEvents back from history-record event dicts; unreadable entries are left out."""
    out: list[MacroEvent] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        at = parse_iso(item.get("event_at"))
        if at is None:
            continue
        extra = dict(item.get("extra") or {})
        out.append(MacroEvent(kind=normalise_kind(item.get("kind")), event_at=at, source=source,
                              title=extra.get("title"), extra=extra,
                              **{k: item.get(k) for k in VALUE_KEYS}))
    return out


# --------------------------------------------------------------------------- merge


def _agree(a: datetime, b: datetime, cfg: ScoutConfig, kind: str) -> bool:
    if et_date(a) != et_date(b):
        return False
    if kind == "fomc":  # FOMC is checked by date only (SCOUT_SPEC 7.3)
        return True
    return abs((a - b).total_seconds()) <= cfg.macro_agree_minutes * 60


def _combine(primary: MacroEvent, other: MacroEvent, agree: bool) -> MacroEvent:
    earlier = min(primary.event_at, other.event_at)
    values = {k: getattr(primary, k) if getattr(primary, k) is not None else getattr(other, k) for k in VALUE_KEYS}
    sources = tuple(sorted({primary.source, other.source}, key=lambda s: _PRIMARY.get(s, 9)))
    return replace(primary, event_at=earlier, title=primary.title or other.title, sources=sources,
                   sources_agree=agree, times={primary.source: iso(primary.event_at), other.source: iso(other.event_at)},
                   extra={**other.extra, **primary.extra}, **values)


def _covers(events: Sequence[MacroEvent], kind: str, at: datetime) -> bool:
    """True when `events` hold `kind` releases on both sides of `at` (or at it): that source was
    read and should have listed a release at `at`."""
    times = [e.event_at for e in events if e.kind == kind]
    return bool(times) and min(times) - PAIR_WINDOW <= at <= max(times) + PAIR_WINDOW


def merge_events(now_events: Sequence[MacroEvent], bls_events: Sequence[MacroEvent],
                 fomc_events: Sequence[MacroEvent], cfg: ScoutConfig) -> list[MacroEvent]:
    """One list of releases, earliest first. Nowflation events are matched with BLS (CPI, NFP) and
    the FOMC table (FOMC): a match on the same ET date within `macro_agree_minutes` agrees; a
    same-kind event within 7 days that does not agree is the same release, disagreeing, and the
    earlier time wins. An event with no match: `sources_agree = False` when the other source was
    read and lists that kind around that time (within 7 days of its first and last), else `None`. PCE and other kinds: `None`."""
    second = {"bls": list(bls_events), "fomc": list(fomc_events)}
    used: set[int] = set()
    merged: list[MacroEvent] = []
    for ev in sorted(now_events, key=lambda e: e.event_at):
        other_name = CROSS_CHECKED.get(ev.kind)
        if other_name is None:
            merged.append(replace(ev, sources=(ev.source,), sources_agree=None, times={ev.source: iso(ev.event_at)}))
            continue
        pool = [(i, o) for i, o in enumerate(second[other_name]) if o.kind == ev.kind and id(o) not in used]
        best = min(pool, key=lambda p: abs((p[1].event_at - ev.event_at).total_seconds()), default=None)
        if best is not None and abs(best[1].event_at - ev.event_at) <= PAIR_WINDOW:
            used.add(id(best[1]))
            merged.append(_combine(ev, best[1], _agree(ev.event_at, best[1].event_at, cfg, ev.kind)))
            continue
        agree = False if _covers(second[other_name], ev.kind, ev.event_at) else None
        merged.append(replace(ev, sources=(ev.source,), sources_agree=agree, times={ev.source: iso(ev.event_at)}))
    for events in second.values():
        for o in events:
            if id(o) in used:
                continue
            agree = False if _covers(now_events, o.kind, o.event_at) else None
            merged.append(replace(o, sources=(o.source,), sources_agree=agree, times={o.source: iso(o.event_at)}))
    merged.sort(key=lambda e: (e.event_at, e.kind))
    return merged


# --------------------------------------------------------------------------- regime and lean


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def inflation_regime(rt: Any, cfg: ScoutConfig) -> str:
    """"hot" when the realtime YoY rose by at least `inflation_hot_pp` over
    `inflation_regime_window_days`, "cooling" when it fell by at least `inflation_cooling_pp`,
    "steady" between, "unknown" without that change. `rt` is a `NowRealtime` or a mapping with
    `change_<n>d` keys. The caller passes None (giving "unknown") when nowflation is not ok."""
    if rt is None:
        return "unknown"
    n = cfg.inflation_regime_window_days
    changes = getattr(rt, "changes", None)
    if isinstance(changes, Mapping):
        change = _number(changes.get(n))
    elif isinstance(rt, Mapping):
        change = _number(rt.get(f"change_{n}d"))
    else:
        change = None
    if change is None:
        return "unknown"
    if change >= cfg.inflation_hot_pp:
        return "hot"
    if change <= -cfg.inflation_cooling_pp:
        return "cooling"
    return "steady"


def _diff(a: Any, b: Any) -> Any:
    """a - b for numbers, or per shared numeric key for two mappings; None otherwise."""
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        out = {k: round(_number(a[k]) - _number(b[k]), 6) for k in sorted(set(a) & set(b))
               if _number(a[k]) is not None and _number(b[k]) is not None}
        return out or None
    x, y = _number(a), _number(b)
    return None if x is None or y is None else round(x - y, 6)


def forecast_lean(cal: Any, forecasts: Any = None) -> list[dict[str, Any]]:
    """Per upcoming calendar event with a nowflation forecast: forecast minus prior and minus street
    (consensus), with nowflation's graded count (`n_graded`). Shown as "informational, untested"
    (SCOUT_SPEC 7.3). A CPI event is matched to the forecasts receipt for its `data_month`: the
    receipt fills a forecast, street or Cleveland value the calendar lacks (the calendar wins when
    both have one) and adds the receipt's status ("provisional" until locked the evening before the
    release). An event with no forecast from either is left out; a missing street gives
    `vs_street = None`."""
    events = getattr(cal, "events", None) if cal is not None else None
    graded = getattr(forecasts, "graded", None) if forecasts is not None else None
    find = getattr(forecasts, "receipt", None)
    out: list[dict[str, Any]] = []
    for ev in events or ():
        rc = find(ev.extra.get("data_month")) if callable(find) and ev.kind == "cpi" else None
        rc = rc if isinstance(rc, Mapping) else {}
        forecast = ev.nowflation_forecast if ev.nowflation_forecast is not None else rc.get("forecast")
        if forecast is None:
            continue
        street = ev.street if ev.street is not None else rc.get("street")
        cleveland = ev.cleveland if ev.cleveland is not None else rc.get("cleveland")
        out.append({"kind": ev.kind, "event_at": iso(ev.event_at), "vs_prior": _diff(forecast, ev.prior),
                    "vs_street": _diff(forecast, street), "graded_prints": graded, "note": LEAN_NOTE,
                    "forecast": forecast, "prior": ev.prior, "street": street,
                    "vs_cleveland": _diff(forecast, cleveland),
                    "print_month": ev.extra.get("data_month") if isinstance(ev.extra.get("data_month"), str) else None,
                    "receipt_status": rc.get("status") if isinstance(rc.get("status"), str) else None})
    return out


# --------------------------------------------------------------------------- freshness of a record


def _next_scheduled(after: datetime, at_t: Any, trading_days_only: bool) -> datetime | None:
    day = et_date(after)
    for _ in range(15):
        if calendar_covers(day) and (not trading_days_only or is_trading_day(day)):
            when = et_datetime(day, at_t)
            if when > after:
                return when
        day = next_trading_day(day) if trading_days_only and calendar_covers(day) else day + timedelta(days=1)
    return None


def stale_after(record: MacroRecord, cfg: ScoutConfig) -> str | None:
    """When the reader stops treating `record` as fresh (UTC ISO), or None (never fresh).
    nowflation: the earlier of `source_generated_at` (the older of the calendar's and realtime.json's
    `generated_at`) + `nowflation_max_age_h` and the next ET day's
    `nowflation_fresh_after_et` (SCOUT_SPEC 7.3). bls: fetched + 2 x its spacing. cboe_vix: one hour
    after the next scheduled read (`vix_read_after_et` on the next trading day). fomc: the last
    table date's decision time. bls_data: fetched + 2 days."""
    if record.status != "ok":
        return None
    fetched = parse_iso(record.fetched_at)
    if record.source == "nowflation":
        gen = parse_iso(record.source_generated_at) or fetched
        if gen is None:
            return None
        by_age = gen + timedelta(hours=cfg.nowflation_max_age_h)
        by_day = et_datetime(et_date(gen) + timedelta(days=1), cfg.nowflation_fresh_after_et)
        return iso(min(by_age, by_day))
    if fetched is None:
        return None
    if record.source == "bls":
        spacing = cfg.sources["bls"].min_spacing_s if "bls" in cfg.sources else 604800.0
        return iso(fetched + timedelta(seconds=2 * max(spacing, 86400.0)))
    if record.source == "bls_data":  # results are read daily; two days without a read is stale
        return iso(fetched + timedelta(days=2))
    if record.source == "cboe_vix":
        nxt = _next_scheduled(fetched, cfg.vix_read_after_et, True)
        return iso(nxt + timedelta(hours=1)) if nxt is not None else None
    if record.source == "fomc":
        if not cfg.fomc_dates:
            return None
        return iso(et_datetime(max(cfg.fomc_dates), cfg.fomc_decision_time_et))
    return None


def fact_id(source: str, guid: str, ticker: str | None) -> str:
    """DESIGN D5: sha256 of "{source}|{guid}|{ticker}" (ticker empty when none); the same formula as
    `scout.facts.make_fact_id`, repeated here so this unit stands alone."""
    return hashlib.sha256(f"{source}|{guid}|{ticker or ''}".encode()).hexdigest()


# --------------------------------------------------------------------------- writer


class MacroWriter:
    """Writes the macro files and `macro_event` facts. Keeps the latest record per source."""

    def __init__(self, paths: ScoutPaths, facts: Any, cfg: ScoutConfig, clock: Clock) -> None:
        self.paths, self.facts, self.cfg, self.clock = paths, facts, cfg, clock
        self.latest: dict[str, dict[str, Any]] = {}
        self.last_ok: dict[str, str] = {}  # source -> first_seen_at of its latest `ok` line
        self._reload()

    def _keep(self, line: dict[str, Any]) -> None:
        source = str(line["source"])
        self.latest[source] = line
        if line.get("status") == "ok" and isinstance(line.get("first_seen_at"), str):
            self.last_ok[source] = line["first_seen_at"]

    def _reload(self) -> None:
        """The last history line per source, so a restart merges with what was read before it, and
        the last `ok` line's time per source (the board's `last_ok` after a restart)."""
        for rec in read_jsonl(self.paths.macro / "history.jsonl"):
            if rec.get("format") == FORMAT and rec.get("source") in MACRO_SOURCES:
                self._keep(rec)

    def write(self, record: MacroRecord) -> int:
        """Append the history line, rewrite today's daily file, add facts for new events.
        Returns the number of new facts."""
        line = record.to_record()
        line["stale_after"] = stale_after(record, self.cfg)
        append_jsonl(self.paths, self.paths.macro / "history.jsonl", line)
        self._keep(line)
        merged = self.merged()
        now = self.clock.now()
        self._write_daily(now, merged)
        return self._add_facts(now, merged)

    def merged(self) -> list[MacroEvent]:
        return merged_from(self.latest, self.cfg)

    def _write_daily(self, now: datetime, merged: list[MacroEvent]) -> None:
        body = daily_body(self.latest, merged, self.cfg, now)
        atomic_write_json(self.paths, self.paths.macro / f"{et_date(now).isoformat()}.json", body)

    def _add_facts(self, now: datetime, merged: list[MacroEvent]) -> int:
        added = 0
        for ev in merged:
            if ev.event_at < now:
                continue
            sources = ev.sources or (ev.source,)
            guid = f"{ev.kind}|{iso(ev.event_at)}|{'+'.join(sources)}|{ev.sources_agree}"
            source = sources[0]
            fid = fact_id(source, guid, None)
            if self.facts.seen(fid):
                continue
            extra = {"times": dict(ev.times), "values": {k: getattr(ev, k) for k in VALUE_KEYS}}
            if "nowflation" in sources:
                extra["attribution"] = ATTRIBUTION
            fact = Fact(fact_id=fid, kind="macro_event", ticker=None, cik=None, cls=None, source=source, feed="macro",
                        guid=guid, url=_source_url(self.cfg, source), title=ev.title or ev.kind.upper(),
                        published_at=None, first_seen_at=iso(now), raw_sha256=None, event_at=iso(ev.event_at),
                        event_kind=ev.kind, event_source="+".join(sources), sources_agree=ev.sources_agree,
                        extra=extra)
            if self.facts.add(fact):
                added += 1
        return added



# --------------------------------------------------------------------------- the daily view


def _fresh_in(latest: Mapping[str, Mapping[str, Any]], source: str, now: datetime) -> Mapping[str, Any] | None:
    rec = latest.get(source)
    if rec is None or rec.get("status") != "ok":
        return None
    until = parse_iso(rec.get("stale_after"))
    return rec if until is not None and now < until else None


def merged_from(latest: Mapping[str, Mapping[str, Any]], cfg: ScoutConfig) -> list[MacroEvent]:
    """The merged event list from the latest history record per source."""
    lists = {s: events_from_dicts((latest.get(s) or {}).get("events") or (), s) for s in ("nowflation", "bls", "fomc")}
    return merge_events(lists["nowflation"], lists["bls"], lists["fomc"], cfg)


def daily_body(latest: Mapping[str, Mapping[str, Any]], merged: Sequence[MacroEvent], cfg: ScoutConfig,
               now: datetime) -> dict[str, Any]:
    """The body of `macro/<ET date>.json` as it stands at `now`."""
    nf = _fresh_in(latest, "nowflation", now)
    regime = (nf or {}).get("inflation_regime") or "unknown"
    vix_rec = latest.get("cboe_vix")
    return {
        "format": FORMAT, "at": iso(now), "inflation_regime": regime if regime in REGIMES else "unknown",
        "regime_status": (latest.get("nowflation") or {}).get("status", "unavailable") if nf is None else "ok",
        "vix": (vix_rec or {}).get("vix"),
        "events": [e.to_merged_dict() for e in merged],
        # the latest BLS results (jobs, unemployment, CPI), whatever their age: record and report only
        "results": (latest.get("bls_data") or {}).get("results"),
        # nowflation's forecast lean, shown in the brief as "informational, untested" (SS 7.3)
        "forecast_lean": [dict(x) for x in (nf or {}).get("forecast_lean") or [] if isinstance(x, Mapping)],
        "sources": {name: {"status": rec.get("status"), "reasons": rec.get("reasons", []),
                           "fetched_at": rec.get("fetched_at")} for name, rec in sorted(latest.items())},
        "attribution": ATTRIBUTION,
    }


def daily_as_of(paths: ScoutPaths, cfg: ScoutConfig, as_of: datetime) -> dict[str, Any] | None:
    """The daily view rebuilt from `macro/history.jsonl` using only lines fetched at or before
    `as_of` (the daily file itself is rewritten through the day, so a past brief cannot use it).
    None when no line is visible."""
    latest: dict[str, dict[str, Any]] = {}
    for rec in read_jsonl(paths.macro / "history.jsonl"):
        if rec.get("format") != FORMAT or rec.get("source") not in MACRO_SOURCES:
            continue
        seen = parse_iso(rec.get("fetched_at")) or parse_iso(rec.get("first_seen_at"))
        if seen is None or seen > as_of:
            continue
        latest[str(rec["source"])] = rec
    if not latest:
        return None
    try:
        merged = merged_from(latest, cfg)
    except (SchemaError, TypeError, ValueError, KeyError):
        merged = []
    return daily_body(latest, merged, cfg, as_of)


def _source_url(cfg: ScoutConfig, source: str) -> str | None:
    if source == "fomc":
        return None
    src = cfg.sources.get(source)
    return src.url if src is not None else None
