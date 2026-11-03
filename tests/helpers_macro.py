"""Test helpers for the macro unit: fixture bytes, source URLs and a fake AppContext."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from helpers_fakes import FakeBoard, FakeFactSink, FakeFetcher, FakeHealthSink, FakeRawSink

from scout.clock import ManualClock
from scout.config import ScoutConfig
from scout.paths import ScoutPaths

FIXTURES = Path(__file__).parent / "fixtures"
HALTS = FIXTURES / "halts"
MACRO = FIXTURES / "macro"

NOW_BASE = "https://nowflation.com"
CAL_URL = f"{NOW_BASE}/data/release_calendar.json"
RT_URL = f"{NOW_BASE}/data/realtime.json"
FC_URL = f"{NOW_BASE}/forecasts/index.json"
ICS_URL = f"{NOW_BASE}/nowflation.ics"
BLS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
VIX_URL = "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
HALTS_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
BLS_DATA_BASE = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
BLS_DATA_SERIES = ("CES0000000001", "LNS14000000", "CUUR0000SA0")


def halts_body(name: str = "tradehalts_SYNTHETIC.xml") -> bytes:
    return (HALTS / name).read_bytes()


def macro_body(name: str) -> bytes:
    return (MACRO / name).read_bytes()


def bls_data_results() -> dict[str, bytes]:
    """The three BLS results replies (synthetic), by URL."""
    return {BLS_DATA_BASE + s: macro_body(f"bls_data_{s}_SYNTHETIC.json") for s in BLS_DATA_SERIES}


def nowflation_results(cal: str = "release_calendar_SYNTHETIC.json", rt: str = "realtime_SYNTHETIC.json",
                       fc: str | None = "forecasts_index_SYNTHETIC.json",
                       ics: str | None = "nowflation_SYNTHETIC.ics") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if cal:
        out[CAL_URL] = macro_body(cal)
    if rt:
        out[RT_URL] = macro_body(rt)
    if fc:
        out[FC_URL] = macro_body(fc)
    if ics:
        out[ICS_URL] = macro_body(ics)
    return out


@dataclass
class FakeCtx:
    """The parts of scout.app.AppContext the macro jobs use."""

    cfg: ScoutConfig
    paths: ScoutPaths
    clock: ManualClock
    fetch: FakeFetcher = field(default_factory=FakeFetcher)
    raw_sink: FakeRawSink = field(default_factory=FakeRawSink)
    fact_sink: FakeFactSink = field(default_factory=FakeFactSink)
    health_sink: FakeHealthSink = field(default_factory=FakeHealthSink)
    board: FakeBoard = field(default_factory=FakeBoard)
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("scout.test.macro"))

    def fetcher(self) -> FakeFetcher:
        return self.fetch

    def raw(self) -> FakeRawSink:
        return self.raw_sink

    def facts(self) -> FakeFactSink:
        return self.fact_sink

    def health(self) -> FakeHealthSink:
        return self.health_sink
