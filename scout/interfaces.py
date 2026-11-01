"""The only cross-unit contracts (DESIGN 2.1). Units import each other only through these Protocols
and the record shapes in `scout.models`."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from scout.models import Fact, FetchResult, HealthRecord, PeriodicInfo, SourceState

__all__ = [
    "FactSink", "Fetcher", "HealthSink", "LlmTransport", "RawSink", "StatusBoard", "SubmissionsSource", "TickerMap",
]


@runtime_checkable
class Fetcher(Protocol):
    def fetch(self, source: str, url: str, *, conditional: bool = True,
              max_bytes: int | None = None) -> FetchResult: ...


@runtime_checkable
class RawSink(Protocol):
    def save(self, source: str, name: str, body: bytes, at: datetime) -> str: ...  # returns sha256


@runtime_checkable
class FactSink(Protocol):
    def add(self, fact: Fact) -> bool: ...  # False when fact_id already stored

    def seen(self, fact_id: str) -> bool: ...


@runtime_checkable
class HealthSink(Protocol):
    def record(self, rec: HealthRecord) -> None: ...


@runtime_checkable
class StatusBoard(Protocol):
    def update(self, source: str, state: SourceState) -> None: ...

    def set_llm(self, enabled: bool, spent_today_usd: float, cap_reached: bool) -> None: ...

    def set_levels(self, current_files: list[str], expires: str | None) -> None: ...


@runtime_checkable
class TickerMap(Protocol):
    def ticker_for_cik(self, cik: int) -> tuple[str, str | None] | None: ...  # (ticker, exchange)

    def cik_for_ticker(self, ticker: str) -> int | None: ...


@runtime_checkable
class SubmissionsSource(Protocol):
    def periodic_info(self, ticker: str, now: datetime) -> PeriodicInfo: ...  # never raises


@runtime_checkable
class LlmTransport(Protocol):
    def create(self, **params: Any) -> Any: ...  # an anthropic Message-like object
