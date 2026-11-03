"""Helpers for the answers unit's tests: build a Scout folder state (params, beats, health, facts,
due records) and stub the sibling reader modules other units build (coverage, halts, jason, macro)
so these tests do not wait for them. Nothing here touches the network."""

from __future__ import annotations

import hashlib
import json
import sys
import types
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from scout_reader import records as R
from scout_reader.gates import gate6_window

from scout.clock import iso
from scout.config import ScoutConfig
from scout.models import DueWindowRecord, Fact, Filing, PeriodicInfo

from helpers_fakes import write_records

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "answers"
GATE6_SOURCES = ("edgar_atom", "prnewswire", "stocktitan", "testwire")  # testwire: the invented second wire (helpers_wire)
BEAT_ONLY_SOURCES = ("nasdaq_halts", "nowflation", "bls", "cboe_vix")
OK_RESULTS = ("ok", "not_modified")


# --------------------------------------------------------------------------- records


def fact(ticker: str | None, *, published: datetime, seen: datetime | None = None, cls: str | None = "fda",
         kind: str = "wire", title: str = "Headline", source: str = "prnewswire", guid: str | None = None,
         **extra: Any) -> dict[str, Any]:
    """A complete Fact record (models.Fact) as stored."""
    seen = seen or published
    guid = guid or f"{source}-{ticker}-{iso(published)}-{title}"
    fid = hashlib.sha256(f"{source}|{guid}|{ticker or ''}".encode()).hexdigest()
    return Fact(fact_id=fid, kind=kind, ticker=ticker, cik=None, cls=cls, source=source, feed=source, guid=guid,
                url=None, title=title, published_at=iso(published), first_seen_at=iso(seen), raw_sha256=None,
                **extra).to_record()


def due_record(ticker: str, computed: datetime, *, status: str = "ok", due_start: str | None = "2026-12-01",
               due_end: str | None = "2027-01-15", overdue: bool | None = False, nt_recent: bool | None = False,
               **extra: Any) -> dict[str, Any]:
    base = {"ticker": ticker, "cik": 1, "computed_at": iso(computed), "status": status,
            "last_periodic_form": "10-Q", "last_periodic_accession": "a", "report_date": "2026-09-30",
            "fiscal_year_end": "1231", "next_period_end": "2026-12-31", "next_is_fiscal_year_end": True,
            "due_start": due_start, "due_end": due_end, "overdue": overdue, "nt_recent": nt_recent,
            "nt_accession": None, "submissions_fetched_at": iso(computed)}
    base.update(extra)
    return DueWindowRecord.from_record({"format": 1, **base}).to_record()


def params_record(cfg: ScoutConfig, at: datetime, **override: Any) -> dict[str, Any]:
    return {"format": 1, "first_seen_at": iso(at), "config_sha256": "test", **cfg.reader_params(), **override}


def write_params(scout_dir: Path, cfg: ScoutConfig, at: datetime, **override: Any) -> None:
    write_records(scout_dir, "params", [params_record(cfg, at, **override)])


def write_beats(scout_dir: Path, start: datetime, end: datetime, *, every: float = 30.0,
                sources: Mapping[str, str] | None = None, gap_open: Mapping[str, bool] | None = None,
                stopped: bool = False) -> None:
    # the macro and halt sources too: the reader checks their beat status (DESIGN 3.2 rule 6)
    statuses = dict(sources) if sources is not None else {s: "ok" for s in (*GATE6_SOURCES, *BEAT_ONLY_SOURCES)}
    gaps = dict(gap_open) if gap_open is not None else {s: False for s in statuses}
    rows = []
    at = start
    while at <= end:
        rows.append({"at": iso(at), "stopped": stopped, "sources": statuses, "gap_open": gaps})
        at += timedelta(seconds=every)
    write_records(scout_dir, "beats", rows)


def write_health(scout_dir: Path, start: datetime, end: datetime, *, every: float = 120.0,
                 sources: Iterable[str] = GATE6_SOURCES, result: str = "ok", overlap: bool | None = True) -> None:
    rows = []
    at = start
    while at <= end:
        for s in sources:
            rows.append({"source": s, "feed": s, "kind": "wire", "enabled": True, "gate6": True,
                         "start": iso(at - timedelta(seconds=1)), "end": iso(at), "result": result, "http_status": 200,
                         "items": 20, "new_items": 0, "overlap": overlap, "spacing_s": 60.0, "gap_before": False,
                         "error": None})
        at += timedelta(seconds=every)
    write_records(scout_dir, "health", rows)


def good_world(scout_dir: Path, cfg: ScoutConfig, as_of: datetime, *, since: datetime | None = None,
               health_every: float = 150.0, params: bool = True) -> None:
    """Params (first seen 60 days before as_of), fresh beats around as_of, and gap-free health from
    `since` (default: 10 minutes before gate 6's window) to as_of."""
    if params:
        write_params(scout_dir, cfg, as_of - timedelta(days=60))
    write_beats(scout_dir, as_of - timedelta(seconds=90), as_of)
    since = since or gate6_window(as_of)[0] - timedelta(minutes=10)
    write_health(scout_dir, since, as_of, every=health_every)


# --------------------------------------------------------------------------- stub modules


def simple_gaps(health: list[dict[str, Any]], source: str, start: datetime, end: datetime, *,
                max_gap_s: float, feeds: Any = None) -> list[dict[str, Any]]:
    """A small stand-in for scout_reader.coverage.gaps (unit `wire`), enough for these tests: a poll
    at or before `start`, one within max_gap of `end`, polls no further apart than max_gap, every
    poll in the window ok and overlapping."""
    polls = sorted(((R.parse_time(r.get("end")), r) for r in health
                    if r.get("source") == source and R.parse_time(r.get("end")) is not None), key=lambda p: p[0])
    out: list[dict[str, Any]] = []
    before = [p for p in polls if p[0] <= start]
    inside = [p for p in polls if start < p[0] <= end]
    if not before:
        out.append({"from": iso(start), "to": iso(start), "why": "no_poll_at_start"})
    for at, rec in inside:
        if rec.get("result") not in OK_RESULTS:
            out.append({"from": iso(at), "to": iso(at), "why": "failed_poll"})
        elif rec.get("overlap") is False:
            out.append({"from": iso(at), "to": iso(at), "why": "no_overlap"})
    times = [p[0] for p in before[-1:] + inside]
    for a, b in zip(times, times[1:]):
        if (b - a).total_seconds() > max_gap_s:
            out.append({"from": iso(a), "to": iso(b), "why": "interval"})
    if times and (end - times[-1]).total_seconds() > max_gap_s:
        out.append({"from": iso(times[-1]), "to": iso(end), "why": "no_poll_at_end"})
    return out


def install_module(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: Any) -> types.ModuleType:
    """Put a fake `scout_reader.<name>` in sys.modules for this test."""
    module = types.ModuleType(f"scout_reader.{name}")
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, f"scout_reader.{name}", module)
    return module


def hide_module(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make `scout_reader.<name>` unimportable for this test (as if not built)."""
    monkeypatch.setitem(sys.modules, f"scout_reader.{name}", None)  # type: ignore[arg-type]


def install_coverage(monkeypatch: pytest.MonkeyPatch, fn: Callable[..., Any] = simple_gaps) -> None:
    install_module(monkeypatch, "coverage", gaps=fn)


def tri(value: str, *reasons: str, **extra: Any) -> dict[str, Any]:
    return {"value": value, "evidence": [], "stale": False, "reasons": list(reasons), **extra}


def install_all_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Coverage plus simple halts / jason / macro stand-ins."""
    install_coverage(monkeypatch)

    def halt_open_from(facts: list[dict[str, Any]], beat: Any, symbol: str, as_of: datetime) -> dict[str, Any]:
        open_ = [f for f in facts if f.get("ticker") == symbol and not f.get("resumed_at")]
        return tri("true" if open_ else "false", evidence=[f["fact_id"] for f in open_])

    def jason_tags_from(snapshot: Any, symbol: str, price: Any, as_of: datetime, near_pct: Any,
                        frame_prices: Any) -> dict[str, Any]:
        if snapshot is None:
            return {k: tri("unknown", "no_snapshot") for k in ("on_jason_list", "near_jason_level", "jason_conflict",
                                                              "jason_market_frame")}
        on = any(s.get("symbol") == symbol for s in snapshot.get("setups", []))
        return {"on_jason_list": tri("true" if on else "false"), "near_jason_level": tri("unknown"),
                "jason_conflict": tri("false"), "jason_market_frame": tri("unknown")}

    def market_context_from(history: list[Any], facts: list[Any], params: Any, as_of: datetime,
                            beat: Any = None) -> dict[str, Any]:
        events = [f for f in facts if f.get("kind") == "macro_event"]
        return {"macro_event_next_session": tri("true" if events else "false"),
                "inflation_regime": tri("unknown"), "vix": tri("unknown"), "next_macro_event": tri("unknown")}

    install_module(monkeypatch, "halts", halt_open_from=halt_open_from)
    install_module(monkeypatch, "jason", jason_tags_from=jason_tags_from)
    install_module(monkeypatch, "macro", market_context_from=market_context_from)


# --------------------------------------------------------------------------- fixtures


def load_submissions(name: str) -> tuple[PeriodicInfo, dict[str, Any]]:
    """A synthetic submissions fixture as PeriodicInfo (status ok) plus the raw JSON."""
    raw = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    filings = tuple(Filing(form=f["form"], accepted=f["accepted"], filing_date=f["filing_date"],
                           accession=f["accession"], items=tuple(f["items"]), report_date=f["report_date"])
                    for f in raw["filings"])
    info = PeriodicInfo(ticker=raw["ticker"], cik=raw["cik"], status="ok", filings=filings,
                        fiscal_year_end=raw["fiscalYearEnd"], fetched_at=None)
    return info, raw
