"""Test helpers for unit `levels`: a level read, a file writer, daily bars and a fake bars loader."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from scout.levels_bars import DailyBar

FIXTURES = Path(__file__).parent / "fixtures" / "levels"
FIXTURE_20260915 = FIXTURES / "jason_levels_20260915.json"
FIXTURE_LIVE = FIXTURES / "jason_levels_live_20260915.json"

#: A Wednesday, the day after a Tuesday read (the old test module's TODAY).
TODAY = date(2026, 9, 16)

#: The old platform's test read (test_phase_ao_jason_levels.READ) plus Scout's source_kind.
READ: dict[str, Any] = {
    "session_date": "2026-09-15",
    "source_kind": "tuesday",
    "source": "Technical Tuesday, Jason at the charts",
    "market": "SPY",
    "accept_minutes": 15,
    "expires_default": "2026-09-19",
    "setups": [
        {"symbol": "NVDA", "direction": "long", "attribution": "jason",
         "confidence": "want to see this bounce aggressively", "horizon": "post-FOMC, days",
         "levels": [
             {"price": "213", "kind": "support", "note": "213 is my support area"},
             {"price": "220", "kind": "trigger", "note": "at least to 220 post FOMC"},
             {"price": "200", "kind": "resistance", "note": "next resistance"},
             {"price": "190", "kind": "invalidation", "note": "sub-200 and I don't mind"},
             {"price": "240", "kind": "target", "note": "objective"},
         ],
         "conditionals": [{"if": "look above and fail", "then": "kind of lame"}]},
        {"symbol": "AMD", "direction": "avoid", "attribution": "jason", "confidence": "semis are done",
         "levels": [{"price": "500", "kind": "invalidation", "note": "lose 500 -> 450"},
                    {"price": "450", "kind": "target", "note": "downside objective"}]},
        {"symbol": "TSLA", "direction": "long", "attribution": "amit", "confidence": "the desk, not Jason",
         "levels": [{"price": "400", "kind": "trigger", "note": "over 400"},
                    {"price": "380", "kind": "invalidation", "note": "under 380"}]},
        {"symbol": "IGV", "direction": "neutral", "attribution": "jason", "confidence": "more concerning",
         "levels": [{"price": "100", "kind": "support", "note": "hold 100"}]},
    ],
}


def read(**overrides: Any) -> dict[str, Any]:
    """A deep copy of READ with top-level overrides."""
    out = copy.deepcopy(READ)
    out.update(overrides)
    return out


def write_read(folder: Path, data: Mapping[str, Any] | str, name: str | None = None) -> Path:
    """Write a level file (a dict as JSON, or raw text) and return its path."""
    folder.mkdir(parents=True, exist_ok=True)
    if name is None:
        if isinstance(data, Mapping):
            name = f"{data.get('session_date', 'x')}.{data.get('source_kind', 'tuesday')}.json"
        else:
            name = "raw.json"
    path = folder / name
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def one_setup(symbol: str, levels: Sequence[Mapping[str, Any]], **setup: Any) -> dict[str, Any]:
    return {"symbol": symbol, "direction": "long", "attribution": "jason", "levels": list(levels), **setup}


def plan_with(setups: Sequence[Mapping[str, Any]], **top: Any) -> dict[str, Any]:
    return {"session_date": "2026-09-15", "source_kind": "tuesday", "setups": list(setups), **top}


def bar(day: str | date, o: object, h: object, lo: object, c: object) -> DailyBar:
    d = day if isinstance(day, date) else date.fromisoformat(day)
    return DailyBar(d, Decimal(str(o)), Decimal(str(h)), Decimal(str(lo)), Decimal(str(c)))


class FakeBars:
    """A BarsLoader over a dict symbol -> bars; records every request."""

    name = "fake"

    def __init__(self, series: Mapping[str, Sequence[DailyBar]] | None = None) -> None:
        self.series = {k.upper(): list(v) for k, v in (series or {}).items()}
        self.calls: list[tuple[str, date, date]] = []
        self.notes: list[str] = []

    def daily(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        self.calls.append((symbol, start, end))
        return [b for b in self.series.get(symbol.upper(), []) if start <= b.day <= end]

    def check(self) -> str | None:
        return None
