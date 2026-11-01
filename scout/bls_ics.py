"""BLS release schedule `.ics` (SCOUT_SPEC 4.1 #8, 7.3; DESIGN 2.3): the CPI and Employment Situation
times that cross-check nowflation's calendar.

`parse_ics` is lenient where real calendars differ (no DTSTAMP, folded lines, CRLF or LF, unknown
properties, a BOM) and strict where a wrong reading would mislead: no VCALENDAR, an event without
DTSTART, an unreadable DTSTART or an unknown TZID is schema drift and refuses the whole source
(`SchemaError`), never a skipped row. ET wall times (`TZID=US-Eastern`, `America/New_York`, ...)
are converted to UTC once, here, via `scout_reader._et`. Nowflation's `.ics` fallback uses the same
parser (`scout.nowflation.parse_ics`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from scout_reader._et import et_to_utc

from scout.clock import Clock, iso
from scout.config import ScoutConfig
from scout.macro import MacroEvent, SchemaError
from scout.models import FetchResult, HealthRecord, MacroRecord, SourceState

log = logging.getLogger("scout.bls")

ET_TZIDS = frozenset({"us-eastern", "america/new_york", "us/eastern", "est5edt", "eastern standard time",
                      "eastern time", "et"})
UTC_TZIDS = frozenset({"utc", "etc/utc", "gmt", "z"})
_DT = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z)?)?$")

# BLS SUMMARY prefixes this unit keeps (others are other releases; counted, not stored).
BLS_KINDS = (("consumer price index", "cpi"), ("employment situation", "nfp"))

__all__ = ["BLS_KINDS", "BlsReader", "IcsEvent", "bls_events", "parse_bls_ics", "parse_ics"]


@dataclass(frozen=True)
class IcsEvent:
    uid: str | None
    summary: str
    start: datetime  # aware UTC; an all-day event starts at 00:00 ET of its date
    all_day: bool
    tz: str  # "et", "utc" or "floating-et"


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)
    return lines


def _split(line: str) -> tuple[str, dict[str, str], str]:
    head, sep, value = line.partition(":")
    if not sep:
        raise SchemaError(f"ics: a content line without ':': {line[:40]!r}")
    parts = head.split(";")
    params: dict[str, str] = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.strip().upper()] = v.strip().strip('"')
    return parts[0].strip().upper(), params, value


def _unescape(value: str) -> str:
    return (value.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";")
            .replace("\\\\", "\\").strip())


def _start(value: str, params: dict[str, str]) -> tuple[datetime, bool, str]:
    m = _DT.match(value.strip())
    if not m:
        raise SchemaError(f"ics: unreadable DTSTART {value[:30]!r}")
    y, mo, d, hh, mm, ss, z = m.groups()
    all_day = hh is None or params.get("VALUE", "").upper() == "DATE"
    try:
        naive = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0))
    except ValueError as exc:
        raise SchemaError(f"ics: impossible DTSTART {value[:30]!r}") from exc
    if z:
        return naive.replace(tzinfo=timezone.utc), all_day, "utc"
    tzid = params.get("TZID", "").strip().lower()
    if tzid in UTC_TZIDS:
        return naive.replace(tzinfo=timezone.utc), all_day, "utc"
    if tzid in ET_TZIDS:
        return et_to_utc(naive), all_day, "et"
    if tzid:
        raise SchemaError(f"ics: unknown TZID {params.get('TZID', '')[:40]!r}")
    return et_to_utc(naive), all_day, "floating-et"  # US release calendars float in ET


def parse_ics(body: bytes) -> list[IcsEvent]:
    """Every VEVENT of an iCalendar body (lenient reader, strict on drift; see module doc)."""
    if not isinstance(body, (bytes, bytearray)):
        raise SchemaError("ics: body is not bytes")
    try:
        text = bytes(body).decode("utf-8-sig")
    except UnicodeDecodeError:
        text = bytes(body).decode("latin-1")
    lines = _unfold(text)
    if not lines or lines[0].strip().upper() != "BEGIN:VCALENDAR":
        raise SchemaError("ics: no BEGIN:VCALENDAR")
    if not any(ln.strip().upper() == "END:VCALENDAR" for ln in lines):
        raise SchemaError("ics: no END:VCALENDAR (truncated?)")
    events: list[IcsEvent] = []
    current: dict[str, Any] | None = None
    for line in lines[1:]:
        upper = line.strip().upper()
        if upper == "BEGIN:VEVENT":
            if current is not None:
                raise SchemaError("ics: nested VEVENT")
            current = {}
            continue
        if upper == "END:VEVENT":
            if current is None:
                raise SchemaError("ics: END:VEVENT without BEGIN")
            if "start" not in current:
                raise SchemaError("ics: VEVENT without DTSTART")
            start, all_day, tz = current["start"]
            events.append(IcsEvent(uid=current.get("uid"), summary=current.get("summary", ""), start=start,
                                   all_day=all_day, tz=tz))
            current = None
            continue
        if current is None:
            continue  # calendar-level properties and other components
        name, params, value = _split(line)
        if name == "DTSTART":
            current["start"] = _start(value, params)
        elif name == "SUMMARY":
            current["summary"] = _unescape(value)
        elif name == "UID":
            current["uid"] = value.strip()
    if current is not None:
        raise SchemaError("ics: VEVENT not closed")
    return events


def parse_bls_ics(body: bytes) -> list[IcsEvent]:
    """The BLS schedule (no DTSTAMP is fine)."""
    return parse_ics(body)


def bls_events(events: list[IcsEvent]) -> tuple[list[MacroEvent], int]:
    """CPI and Employment Situation releases as MacroEvents (source "bls"); and how many other
    events were left out."""
    out: list[MacroEvent] = []
    other = 0
    for ev in events:
        summary = ev.summary.strip().lower()
        kind = next((k for prefix, k in BLS_KINDS if summary.startswith(prefix)), None)
        if kind is None or ev.all_day:
            other += 1
            continue
        out.append(MacroEvent(kind=kind, event_at=ev.start, source="bls", title=ev.summary,
                              extra={"uid": ev.uid} if ev.uid else {}))
    out.sort(key=lambda e: e.event_at)
    return out, other


class BlsReader:
    """Reads the BLS `.ics` (weekly) into a MacroRecord (source "bls")."""

    def __init__(self, cfg: ScoutConfig, fetcher: Any, raw: Any, health: Any, board: Any, clock: Clock) -> None:
        self.cfg, self.fetcher, self.raw, self.health, self.board, self.clock = cfg, fetcher, raw, health, board, clock
        self.last_ok: str | None = None

    def read(self, now: datetime) -> MacroRecord:
        src = self.cfg.source("bls")
        got: FetchResult = self.fetcher.fetch("bls", src.url, conditional=False)
        reasons: list[str] = []
        events: list[MacroEvent] = []
        sha: str | None = None
        result, error, items = got.result, got.error, 0
        status = "unavailable"
        if got.ok and got.body is not None:
            sha = self.raw.save("bls", "bls.ics", got.body, now)
            try:
                parsed = parse_bls_ics(got.body)
                events, other = bls_events(parsed)
                items = len(parsed)
                ahead = {e.kind for e in events if e.event_at > now}
                status = "ok" if {"cpi", "nfp"} <= ahead else "stale"
                if not events:
                    reasons.append("no_cpi_or_nfp_events")
                elif status != "ok":
                    # an old schedule (a year-end rollover, a stale file) must not pass as fresh:
                    # the reader's macro fallback would answer false with nothing known ahead
                    reasons.append("no_cpi_and_nfp_after_now")
                if other:
                    reasons.append(f"other_events_left_out:{other}")
            except SchemaError as exc:
                result, error = "schema", str(exc)[:200]
                reasons.append("schema")
        else:
            reasons.append(f"fetch:{got.result}")
        _health(self.health, self.cfg, "bls", "bls.ics", got, result, items, error)
        if status == "ok":
            self.last_ok = iso(now)
        _board(self.board, self.cfg, "bls", status, self.last_ok, error or (None if status == "ok" else reasons[0]))
        return MacroRecord(source="bls", first_seen_at=iso(now), fetched_at=got.ended_at, status=status,
                           reasons=tuple(reasons), source_generated_at=None, sha256=sha,
                           events=tuple(e.to_dict() for e in events), realtime=None, inflation_regime=None,
                           forecast_lean=(), vix=None, attribution=None)


# --------------------------------------------------------------------------- shared by the macro readers


def _health(health: Any, cfg: ScoutConfig, source: str, feed: str, got: FetchResult, result: str, items: int,
            error: str | None) -> None:
    """One health record per fetch (DESIGN 5.2). Macro sources take no part in gate 6 or overlap."""
    if health is None:
        return
    src = cfg.sources.get(source)
    try:
        health.record(HealthRecord(source=source, feed=feed, kind=src.kind if src else "macro",
                                   enabled=bool(src.enabled) if src else True, gate6=False, start=got.started_at,
                                   end=got.ended_at, result=result, http_status=got.status, items=items,
                                   new_items=0, overlap=None, spacing_s=float(src.min_spacing_s) if src else 0.0,
                                   gap_before=False, error=error))
    except Exception as exc:  # noqa: BLE001 - health is a record; it never stops a read
        log.warning("health record for %s failed: %s", source, type(exc).__name__)


_BOARD_STATUS = {"ok": "ok", "stale": "stale", "unavailable": "unavailable"}


def _board(board: Any, cfg: ScoutConfig, source: str, status: str, last_ok: str | None, last_error: str | None) -> None:
    if board is None:
        return
    src = cfg.sources.get(source)
    board.update(source, SourceState(enabled=bool(src.enabled) if src else True,
                                     status=_BOARD_STATUS.get(status, "unavailable"), last_ok=last_ok,
                                     last_error=None if status == "ok" else last_error, gap_open=False))
