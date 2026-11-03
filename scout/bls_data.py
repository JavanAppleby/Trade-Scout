"""BLS results: the jobs report and CPI as published (owner's request of 2026-11-04). Record only.

Reads the BLS Public Data API v1 (`api.bls.gov/publicAPI/v1/timeseries/data/<series>`; no key, no
login) for three fixed series, one request each:

| Series | What | Kept as |
|---|---|---|
| CES0000000001 | total nonfarm payrolls, thousands, seasonally adjusted | `payrolls`: month, change_k, prior_change_k, preliminary |
| LNS14000000 | unemployment rate, %, seasonally adjusted | `unemployment`: month, rate, prior |
| CUUR0000SA0 | CPI-U all items, index, not seasonally adjusted | `cpi`: month, yoy_pct, prior_yoy_pct (headline year-on-year, to 0.1) |

Each result carries what was expected before it came out, from the last nowflation history line
first seen before this read (street for jobs; street and nowflation's forecast for CPI), and the
miss or beat against it (`vs_street`, `vs_nowflation`). `jobs_trend` labels the average monthly
change over the last `jobs_trend_months` months: "strong" at or above `jobs_strong_k`, "weak" at or
below `jobs_weak_k`, "steady" between, "unknown" when a month is missing.

Pinned schema: `status` and `Results.series[0]` with the asked `seriesID` and a `data` list of
`year`, `period`, `value`. Periods M01-M12 are months; M13 (BLS's annual average) is not a month and
is left out by design. A value "-" is BLS's own "not available" marker (no month kept). Anything
else is drift: `SchemaError`, the source is refused. A reply whose `status` is not
"REQUEST_SUCCEEDED" (for example BLS's daily limit) is `unavailable` with reason `bls_refused`.

Nothing here gates a hold or changes an answer: it is shown in the brief and the dashboard, and kept
in the macro history (first seen by Scout's clock) for later analysis against the trade journals.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from scout.bls_ics import _board, _health
from scout.clock import Clock, et_date, iso, parse_iso
from scout.config import ScoutConfig
from scout.macro import SchemaError
from scout.models import FetchResult, MacroRecord

log = logging.getLogger("scout.bls_data")

SOURCE = "bls_data"
SERIES = {"payrolls": "CES0000000001", "unemployment": "LNS14000000", "cpi": "CUUR0000SA0"}
EXPECTED_KIND = {"payrolls": "nfp", "cpi": "cpi"}  # result -> nowflation event kind
MAX_LAG_MONTHS = 2  # the newest month may trail today's month by this much before the record is stale
TRENDS = ("strong", "steady", "weak", "unknown")

__all__ = ["SERIES", "SOURCE", "BlsDataReader", "MonthValue", "expected", "jobs_trend", "parse_series",
           "results_from", "summary_lines"]


@dataclass(frozen=True)
class MonthValue:
    month: str  # "YYYY-MM"
    value: float | None  # None: BLS marked the month "-"
    preliminary: bool


def _month_key(year: Any, period: Any, where: str) -> str | None:
    if not isinstance(year, str) or not isinstance(period, str) or len(year) != 4 or not year.isdigit():
        raise SchemaError(f"{where}: unreadable year or period")
    if period == "M13":
        return None
    if len(period) != 3 or period[0] != "M" or not period[1:].isdigit() or not 1 <= int(period[1:]) <= 12:
        raise SchemaError(f"{where}: period {period[:8]!r} is not a month")
    return f"{year}-{period[1:]}"


def parse_series(body: bytes, series_id: str) -> list[MonthValue] | None:
    """The months of `series_id`, newest first. None when BLS refused the request (its `status`)."""
    try:
        obj = json.loads(bytes(body).decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise SchemaError(f"{series_id}: not JSON") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("status"), str):
        raise SchemaError(f"{series_id}: no status")
    if obj["status"] != "REQUEST_SUCCEEDED":
        return None
    results = obj.get("Results")
    series = results.get("series") if isinstance(results, dict) else None
    if not isinstance(series, list) or len(series) != 1 or not isinstance(series[0], dict):
        raise SchemaError(f"{series_id}: Results.series is not one series")
    if series[0].get("seriesID") != series_id:
        raise SchemaError(f"{series_id}: the reply is for another series")
    data = series[0].get("data")
    if not isinstance(data, list):
        raise SchemaError(f"{series_id}: data is not a list")
    out: dict[str, MonthValue] = {}
    for i, row in enumerate(data):
        where = f"{series_id} data[{i}]"
        if not isinstance(row, dict):
            raise SchemaError(f"{where}: not an object")
        month = _month_key(row.get("year"), row.get("period"), where)
        if month is None:
            continue
        raw = row.get("value")
        if not isinstance(raw, str):
            raise SchemaError(f"{where}: value is not text")
        value: float | None = None
        if raw.strip() != "-":
            try:
                value = float(raw.replace(",", ""))
            except ValueError as exc:
                raise SchemaError(f"{where}: value is not a number") from exc
            if not math.isfinite(value):
                raise SchemaError(f"{where}: value out of range")
        notes = row.get("footnotes") or []
        prelim = isinstance(notes, list) and any(isinstance(n, dict) and n.get("code") == "P" for n in notes)
        if month in out:
            raise SchemaError(f"{where}: month {month} twice")
        out[month] = MonthValue(month=month, value=value, preliminary=prelim)
    if not any(v.value is not None for v in out.values()):
        raise SchemaError(f"{series_id}: no months")
    return sorted(out.values(), key=lambda v: v.month, reverse=True)


def _shift(month: str, back: int) -> str:
    y, m = int(month[:4]), int(month[5:])
    n = y * 12 + (m - 1) - back
    return f"{n // 12:04d}-{n % 12 + 1:02d}"


def _value(rows: Mapping[str, MonthValue], month: str) -> float | None:
    row = rows.get(month)
    return row.value if row is not None else None


def _sub(a: float | None, b: float | None, places: int) -> float | None:
    return None if a is None or b is None else round(a - b, places)


def jobs_trend(changes: Iterable[float | None], cfg: ScoutConfig) -> str:
    vals = list(changes)
    if len(vals) < cfg.jobs_trend_months or any(v is None for v in vals):
        return "unknown"
    avg = sum(v for v in vals if v is not None) / len(vals)
    return "strong" if avg >= cfg.jobs_strong_k else "weak" if avg <= cfg.jobs_weak_k else "steady"


def expected(history: Iterable[Mapping[str, Any]], kind: str, month: str, before: datetime) -> dict[str, Any]:
    """Street and nowflation forecast for the `kind` release of data month `month`, from the last
    nowflation history line first seen before `before` that lists it (no look-ahead)."""
    best: tuple[datetime, dict[str, Any]] | None = None
    for line in history:
        if not isinstance(line, Mapping) or line.get("source") != "nowflation":
            continue
        seen = parse_iso(line.get("first_seen_at"))
        if seen is None or seen >= before:
            continue
        for ev in line.get("events") or ():
            if (isinstance(ev, Mapping) and ev.get("kind") == kind
                    and isinstance(ev.get("extra"), Mapping) and ev["extra"].get("data_month") == month):
                if best is None or seen > best[0]:
                    best = (seen, {"street": ev.get("street"), "nowflation_forecast": ev.get("nowflation_forecast"),
                                   "as_of": iso(seen)})
    return best[1] if best else {}


def _number(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def results_from(series: Mapping[str, list[MonthValue]], cfg: ScoutConfig,
                 expect: Callable[[str, str], Mapping[str, Any]]) -> dict[str, Any]:
    """The `results` of a read from the parsed series (any of the three may be missing)."""
    out: dict[str, Any] = {}
    pay = {r.month: r for r in series.get("payrolls") or ()}
    known = [r for r in series.get("payrolls") or () if r.value is not None]
    if known:
        m = known[0].month
        changes = [_sub(_value(pay, _shift(m, i)), _value(pay, _shift(m, i + 1)), 0)
                   for i in range(max(cfg.jobs_trend_months, 2))]
        window = changes[:cfg.jobs_trend_months]
        exp = expect("nfp", m)
        street = _number(exp.get("street"))
        out["payrolls"] = {"month": m, "change_k": changes[0], "prior_change_k": changes[1],
                           "preliminary": known[0].preliminary, "street_k": street,
                           "vs_street_k": _sub(changes[0], street, 0), "expected_as_of": exp.get("as_of")}
        out["jobs_trend"] = jobs_trend(window, cfg)
        known_window = [v for v in window if v is not None]
        out["jobs_trend_avg_k"] = round(sum(known_window) / len(window)) if len(known_window) == len(window) else None
    rate = [r for r in series.get("unemployment") or () if r.value is not None]
    if rate:
        rows = {r.month: r for r in rate}
        m = rate[0].month
        out["unemployment"] = {"month": m, "rate": rate[0].value, "prior": _value(rows, _shift(m, 1))}
    cpi = {r.month: r for r in series.get("cpi") or ()}
    cpi_known = [r for r in series.get("cpi") or () if r.value is not None]
    if cpi_known:
        m = cpi_known[0].month

        def yoy(month: str) -> float | None:
            now_v, then_v = _value(cpi, month), _value(cpi, _shift(month, 12))
            return round((now_v / then_v - 1) * 100, 1) if now_v is not None and then_v else None

        value = yoy(m)
        exp = expect("cpi", m)
        street, nf = _number(exp.get("street")), _number(exp.get("nowflation_forecast"))
        out["cpi"] = {"month": m, "yoy_pct": value, "prior_yoy_pct": yoy(_shift(m, 1)), "street": street,
                      "nowflation_forecast": nf, "vs_street": _sub(value, street, 2),
                      "vs_nowflation": _sub(value, nf, 2), "expected_as_of": exp.get("as_of")}
    return out


def _lag(month: str, today: date) -> int:
    return (today.year * 12 + today.month) - (int(month[:4]) * 12 + int(month[5:]))


class BlsDataReader:
    """Reads the three series into one MacroRecord (source "bls_data")."""

    def __init__(self, cfg: ScoutConfig, fetcher: Any, raw: Any, health: Any, board: Any, clock: Clock,
                 history: Callable[[], Iterable[Mapping[str, Any]]] = tuple) -> None:
        self.cfg, self.fetcher, self.raw, self.health, self.board, self.clock = cfg, fetcher, raw, health, board, clock
        self.history = history
        self.last_ok: str | None = None

    def read(self, now: datetime) -> MacroRecord:
        base = self.cfg.source(SOURCE).url.rstrip("/") + "/"
        parsed: dict[str, list[MonthValue]] = {}
        reasons: list[str] = []
        sha: str | None = None
        fetched_at = iso(now)
        for name, sid in SERIES.items():
            got: FetchResult = self.fetcher.fetch(SOURCE, base + sid, conditional=False)
            fetched_at = got.ended_at or fetched_at
            result, error, items = got.result, got.error, 0
            if got.ok and got.body is not None:
                sha = self.raw.save(SOURCE, f"{sid}.json", got.body, now)
                try:
                    rows = parse_series(got.body, sid)
                    if rows is None:
                        result, error = "refused", "BLS did not process the request"
                        reasons.append("bls_refused")
                    else:
                        parsed[name], items = rows, len(rows)
                except SchemaError as exc:
                    result, error = "schema", str(exc)[:200]
                    reasons.append(f"schema:{name}")
            else:
                reasons.append(f"fetch:{got.result}")
            _health(self.health, self.cfg, SOURCE, sid, got, result, items, error)
        try:
            history = list(self.history())
        except Exception as exc:  # noqa: BLE001 - expectations are extra; a bad history never stops a read
            log.warning("macro history unreadable for expectations: %s", type(exc).__name__)
            history = []
        results = results_from(parsed, self.cfg, lambda kind, month: expected(history, kind, month, now))
        status = "ok" if len(parsed) == len(SERIES) else "unavailable" if not parsed else "stale"
        if status == "ok":
            today = et_date(now)
            behind = [n for n in ("payrolls", "unemployment", "cpi")
                      if _lag(str(results.get(n, {}).get("month") or "0000-01"), today) > MAX_LAG_MONTHS]
            if behind:
                status = "stale"
                reasons.extend(f"old_month:{n}" for n in behind)
        if status == "ok":
            self.last_ok = iso(now)
        _board(self.board, self.cfg, SOURCE, status, self.last_ok, reasons[0] if reasons else None)
        return MacroRecord(source=SOURCE, first_seen_at=iso(now), fetched_at=fetched_at, status=status,
                           reasons=tuple(dict.fromkeys(reasons)), source_generated_at=None, sha256=sha, events=(),
                           realtime=None, inflation_regime=None, forecast_lean=(), vix=None, attribution=None,
                           results=results or None)


# --------------------------------------------------------------------------- report text


def _month_name(month: Any) -> str:
    try:
        return date(int(str(month)[:4]), int(str(month)[5:7]), 1).strftime("%B %Y")
    except ValueError:
        return "an unknown month"


def _k(v: Any) -> str:
    return f"{v:+,.0f}k" if isinstance(v, (int, float)) and not isinstance(v, bool) else "n/a"


def summary_lines(results: Mapping[str, Any] | None) -> list[str]:
    """Plain-English lines for the brief (no Markdown from the data: every value is a number or a
    fixed word)."""
    if not isinstance(results, Mapping) or not results:
        return ["- No BLS results yet."]
    out = []
    pay = results.get("payrolls")
    if isinstance(pay, Mapping):
        line = f"- Jobs, {_month_name(pay.get('month'))}: {_k(pay.get('change_k'))}"
        if pay.get("preliminary"):
            line += " (first estimate)"
        if isinstance(pay.get("street_k"), (int, float)):
            miss = pay.get("vs_street_k")
            word = "a beat" if isinstance(miss, (int, float)) and miss > 0 else "a miss" if isinstance(
                miss, (int, float)) and miss < 0 else "in line"
            line += f" vs {_k(pay['street_k'])} expected ({word}, {_k(miss)})"
        line += f"; month before {_k(pay.get('prior_change_k'))}"
        out.append(line)
    trend = results.get("jobs_trend")
    if trend in TRENDS:
        avg = results.get("jobs_trend_avg_k")
        out.append(f"- Jobs trend: {trend}" + (f" (average {_k(avg)} a month)" if avg is not None else ""))
    un = results.get("unemployment")
    if isinstance(un, Mapping):
        out.append(f"- Unemployment, {_month_name(un.get('month'))}: {un.get('rate')}%"
                   + (f" (before: {un.get('prior')}%)" if un.get("prior") is not None else ""))
    cpi = results.get("cpi")
    if isinstance(cpi, Mapping):
        line = f"- CPI, {_month_name(cpi.get('month'))}: {cpi.get('yoy_pct')}% a year"
        if cpi.get("prior_yoy_pct") is not None:
            line += f" (before: {cpi.get('prior_yoy_pct')}%)"
        if cpi.get("vs_nowflation") is not None:
            line += f"; Nowflation's forecast was {cpi.get('nowflation_forecast')}% (off by {cpi['vs_nowflation']:+g})"
        if cpi.get("vs_street") is not None:
            line += f"; street {cpi.get('street')}% ({cpi['vs_street']:+g})"
        out.append(line)
    return out or ["- No BLS results yet."]
