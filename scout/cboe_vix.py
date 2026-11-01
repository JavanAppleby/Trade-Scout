"""Cboe `VIX_History.csv`: the daily VIX close (SCOUT_SPEC 4.1 #10, 7.6; DESIGN 2.3). Record only.

Pinned schema: the header must be exactly `DATE,OPEN,HIGH,LOW,CLOSE` (a BOM and surrounding spaces
are tolerated); every row must have five fields, a `MM/DD/YYYY` date (ISO `YYYY-MM-DD` also read) and
numeric prices. Anything else is drift: `SchemaError`, the source is refused, rows are never
skipped. The record keeps the latest close with its date; it is `stale` when that date is older than
the previous trading day.
"""

from __future__ import annotations

import csv
import io
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from scout_reader.calendar import calendar_covers, previous_trading_day

from scout.bls_ics import _board, _health
from scout.clock import Clock, et_date, iso
from scout.config import ScoutConfig
from scout.macro import SchemaError
from scout.models import FetchResult, MacroRecord

log = logging.getLogger("scout.cboe_vix")

HEADER = ("DATE", "OPEN", "HIGH", "LOW", "CLOSE")

__all__ = ["HEADER", "VixReader", "VixRow", "latest", "parse_vix_csv"]


@dataclass(frozen=True)
class VixRow:
    day: date
    open: float
    high: float
    low: float
    close: float


def _date(text: str, line: int) -> date:
    t = text.strip()
    try:
        if "/" in t:
            m, d, y = t.split("/")
            return date(int(y), int(m), int(d))
        return date.fromisoformat(t)
    except ValueError as exc:
        raise SchemaError(f"vix line {line}: unreadable DATE {t[:20]!r}") from exc


def _price(text: str, line: int, name: str) -> float:
    try:
        v = float(text.strip())
    except ValueError as exc:
        raise SchemaError(f"vix line {line}: {name} is not a number") from exc
    if not math.isfinite(v) or v < 0:
        raise SchemaError(f"vix line {line}: {name} out of range")
    return v


def parse_vix_csv(body: bytes) -> list[VixRow]:
    """Every row, in file order (no order is relied on later)."""
    if not isinstance(body, (bytes, bytearray)):
        raise SchemaError("vix: body is not bytes")
    try:
        text = bytes(body).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SchemaError("vix: not UTF-8") from exc
    rows = list(csv.reader(io.StringIO(text)))
    while rows and not any(c.strip() for c in rows[-1]):
        rows.pop()
    if not rows or tuple(c.strip() for c in rows[0]) != HEADER:
        raise SchemaError("vix: header is not DATE,OPEN,HIGH,LOW,CLOSE")
    out: list[VixRow] = []
    for i, row in enumerate(rows[1:], start=2):
        if len(row) != 5:
            raise SchemaError(f"vix line {i}: expected 5 fields, got {len(row)}")
        out.append(VixRow(day=_date(row[0], i), open=_price(row[1], i, "OPEN"), high=_price(row[2], i, "HIGH"),
                          low=_price(row[3], i, "LOW"), close=_price(row[4], i, "CLOSE")))
    if not out:
        raise SchemaError("vix: no rows")
    return out


def latest(rows: list[VixRow]) -> VixRow:
    return max(rows, key=lambda r: r.day)


class VixReader:
    """Reads the CSV into a MacroRecord (source "cboe_vix")."""

    def __init__(self, cfg: ScoutConfig, fetcher: Any, raw: Any, health: Any, board: Any, clock: Clock) -> None:
        self.cfg, self.fetcher, self.raw, self.health, self.board, self.clock = cfg, fetcher, raw, health, board, clock
        self.last_ok: str | None = None

    def read(self, now: datetime) -> MacroRecord:
        src = self.cfg.source("cboe_vix")
        got: FetchResult = self.fetcher.fetch("cboe_vix", src.url, conditional=False)
        reasons: list[str] = []
        status, vix, sha, items, result, error = "unavailable", None, None, 0, got.result, got.error
        if got.ok and got.body is not None:
            sha = self.raw.save("cboe_vix", "VIX_History.csv", got.body, now)
            try:
                rows = parse_vix_csv(got.body)
                items = len(rows)
                last = latest(rows)
                vix = {"date": last.day.isoformat(), "close": last.close}
                today = et_date(now)
                floor = previous_trading_day(today) if calendar_covers(today) else None
                if floor is None or last.day < floor:
                    status = "stale"
                    reasons.append("vix_close_older_than_previous_trading_day" if floor else "calendar_not_covered")
                else:
                    status = "ok"
            except SchemaError as exc:
                result, error = "schema", str(exc)[:200]
                reasons.append("schema")
        else:
            reasons.append(f"fetch:{got.result}")
        _health(self.health, self.cfg, "cboe_vix", "VIX_History.csv", got, result, items, error)
        if status == "ok":
            self.last_ok = iso(now)
        _board(self.board, self.cfg, "cboe_vix", status, self.last_ok, reasons[0] if reasons else None)
        return MacroRecord(source="cboe_vix", first_seen_at=iso(now), fetched_at=got.ended_at, status=status,
                           reasons=tuple(reasons), source_generated_at=None, sha256=sha, events=(), realtime=None,
                           inflation_regime=None, forecast_lean=(), vix=vix, attribution=None)
