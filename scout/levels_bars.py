"""Daily bars for the levels scorer, from an injected loader (SCOUT_SPEC 5.4, DESIGN 2.4).

Never Alpaca, eToro, Webull or the network. Two loaders:
- `RadarBars(radar_dir)`: Radar's SQLite (`<radar_dir>\\state\\radar.db`, table `bars`, daily rows
  have `minutes = 1440`) opened read-only by URI (`mode=ro`), open-read-close. Radar keeps mostly
  intraday bars of small caps for `raw_keep_days`; when it holds no daily bars the loader says so
  ("no daily bars") instead of scoring nothing silently.
- `CsvBars(folder)`: owner-supplied `<SYMBOL>.csv` with header `date,open,high,low,close` (extra
  columns are ignored; case-insensitive).

A bad row is skipped; a bad file or database gives no bars and a note. Nothing here writes.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = ["DAY_MINUTES", "BarsLoader", "CsvBars", "DailyBar", "RadarBars", "bar_from_values"]

#: Radar stores a daily bar with `minutes = 1440` (radar/replay.py DAY_MINUTES).
DAY_MINUTES = 1440
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,15}$")


@dataclass(frozen=True)
class DailyBar:
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@runtime_checkable
class BarsLoader(Protocol):
    """Daily bars of `symbol` with `start <= day <= end`, oldest first."""

    def daily(self, symbol: str, start: date, end: date) -> list[DailyBar]: ...


def _price(value: object) -> Decimal | None:
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    return out if out.is_finite() and out > 0 else None


def bar_from_values(day: object, o: object, h: object, lo: object, c: object) -> DailyBar | None:
    """A bar when every field reads and high >= low; else None."""
    try:
        d = day if isinstance(day, date) else date.fromisoformat(str(day).strip()[:10])
    except ValueError:
        return None
    values = [_price(v) for v in (o, h, lo, c)]
    if any(v is None for v in values):
        return None
    op, hi, low, cl = values  # type: ignore[misc]
    if hi < low:  # type: ignore[operator]
        return None
    return DailyBar(d, op, hi, low, cl)  # type: ignore[arg-type]


def _clean_symbol(symbol: str) -> str | None:
    name = str(symbol or "").strip().upper()
    return name if _SYMBOL.match(name) else None


def _dedupe(bars: list[DailyBar]) -> list[DailyBar]:
    by_day: dict[date, DailyBar] = {}
    for bar in bars:
        by_day[bar.day] = bar  # the last row for a day wins
    return [by_day[d] for d in sorted(by_day)]


class RadarBars:
    """Radar's stored daily bars, read-only. `notes` collects what could not be read."""

    name = "radar"

    def __init__(self, radar_dir: Path | str) -> None:
        self.db_path = Path(radar_dir) / "state" / "radar.db"
        self.notes: list[str] = []

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise FileNotFoundError(f"Radar's database is not at {self.db_path}")
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=5.0)

    def _note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def daily_bar_count(self) -> int | None:
        """How many daily rows Radar holds; None when the database cannot be read."""
        try:
            conn = self._connect()
        except (OSError, sqlite3.Error) as exc:
            self._note(f"Radar's bars could not be opened: {type(exc).__name__}: {exc}")
            return None
        try:
            row = conn.execute("SELECT COUNT(*) FROM bars WHERE minutes = ?", (DAY_MINUTES,)).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error as exc:
            self._note(f"Radar's bars could not be read: {type(exc).__name__}: {exc}")
            return None
        finally:
            conn.close()

    def check(self) -> str | None:
        """A plain-English problem with this source, or None when it holds daily bars."""
        count = self.daily_bar_count()
        if count is None:
            return self.notes[-1] if self.notes else "Radar's bars could not be read"
        if count == 0:
            self._note("no daily bars: Radar's database holds no daily (1440-minute) bars")
            return "no daily bars: Radar's database holds no daily (1440-minute) bars"
        return None

    def daily(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        name = _clean_symbol(symbol)
        if name is None:
            return []
        try:
            conn = self._connect()
        except (OSError, sqlite3.Error) as exc:
            self._note(f"Radar's bars could not be opened: {type(exc).__name__}: {exc}")
            return []
        try:
            rows = conn.execute(
                "SELECT trading_day, open, high, low, close FROM bars WHERE symbol = ? AND minutes = ? "
                "AND trading_day >= ? AND trading_day <= ? ORDER BY trading_day, start",
                (name, DAY_MINUTES, start.isoformat(), end.isoformat())).fetchall()
        except sqlite3.Error as exc:
            self._note(f"Radar's bars could not be read: {type(exc).__name__}: {exc}")
            return []
        finally:
            conn.close()
        bars = [b for b in (bar_from_values(*row) for row in rows) if b is not None]
        return _dedupe(bars)


class CsvBars:
    """Owner-supplied daily bars: `<folder>\\<SYMBOL>.csv` with header `date,open,high,low,close`."""

    name = "csv"
    REQUIRED = ("date", "open", "high", "low", "close")

    def __init__(self, folder: Path | str) -> None:
        self.folder = Path(folder)
        self.notes: list[str] = []

    def check(self) -> str | None:
        if not self.folder.is_dir():
            return f"the CSV folder {self.folder} does not exist"
        return None

    def daily(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        name = _clean_symbol(symbol)
        if name is None:
            return []
        path = self.folder / f"{name}.csv"
        try:
            with open(path, encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                header = {h.strip().lower(): h for h in (reader.fieldnames or [])}
                if any(k not in header for k in self.REQUIRED):
                    self.notes.append(f"{path.name}: header must include {','.join(self.REQUIRED)}")
                    return []
                bars: list[DailyBar] = []
                for row in reader:
                    bar = bar_from_values(*(row.get(header[k]) for k in self.REQUIRED))
                    if bar is not None and start <= bar.day <= end:
                        bars.append(bar)
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            self.notes.append(f"{path.name}: unreadable ({type(exc).__name__})")
            return []
        return _dedupe(bars)
