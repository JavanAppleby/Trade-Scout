"""EDGAR reference data (SCOUT_SPEC 4.1 #2-3; DESIGN 2.2).

`TickerMapClient` (implements TickerMap): `company_tickers_exchange.json`, read at start and again
once per ET day from 03:50 ET (`ensure`), kept in memory and in the raw store. When a read fails
the last stored copy is used (it is at most days old; the CIK of a listed company does not change),
and a failed read is tried again after `ticker_map_retry_s` (default `RETRY_S`).

A ticker listed under more than one CIK is `ambiguous`: `cik_for_ticker` still answers with the
first row (Radar's rule), but `SubmissionsClient` refuses to build a due window from it, because the
wrong company's 10-Q cycle could make gate 5 answer a confident false. A CIK with several tickers
(dual class, units and warrants) keeps all of them, in file order (`tickers_for_cik`), so a filing
becomes one fact per ticker.

`SubmissionsClient` (implements SubmissionsSource): `data.sec.gov/submissions/CIK##########.json`
on demand, cached per CIK per ET day. `periodic_info` never raises; its status is `ok`, `no_cik`
(no CIK for the ticker, or EDGAR has none), `ambiguous_ticker` (the ticker is listed under more
than one CIK), `fetch_failed`, `schema` or `no_http`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from scout.clock import Clock, et_date, et_datetime, iso
from scout.config import ScoutConfig
from scout.edgar_parse import SchemaError, parse_json, parse_periodic, parse_ticker_rows
from scout.health import CoverageTracker, board_state
from scout.interfaces import Fetcher, HealthSink, RawSink, StatusBoard, TickerMap
from scout.models import FetchResult, PeriodicInfo
from scout.tickers import normalise

__all__ = ["REFRESH_AT_ET", "RETRY_S", "SubmissionsClient", "TickerMapClient", "build_submissions",
           "build_ticker_map"]

TICKERS = "edgar_tickers"
SUBMISSIONS = "edgar_submissions"
RAW_NAME = "company_tickers_exchange.json"
REFRESH_AT_ET = time(3, 50)
RETRY_S = 600.0

log = logging.getLogger("scout.edgar")


class TickerMapClient:
    def __init__(self, cfg: ScoutConfig, fetcher: Fetcher, raw: RawSink, clock: Clock, *,
                 health: HealthSink | None = None, board: StatusBoard | None = None,
                 tracker: CoverageTracker | None = None) -> None:
        self.cfg = cfg
        self.fetcher, self.raw, self.clock = fetcher, raw, clock
        self.health, self.board = health, board
        self.tracker = tracker or CoverageTracker(cfg)
        self._by_ticker: dict[str, tuple[int, str | None]] = {}
        self._by_cik: dict[int, list[tuple[str, str | None]]] = {}
        self._ambiguous: frozenset[str] = frozenset()
        self.loaded_for: date | None = None  # the ET day of the last good read
        self.from_store = False
        self._last_try: datetime | None = None
        self.last_ok: str | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------ TickerMap

    def ready(self) -> bool:
        return bool(self._by_ticker)

    def ticker_for_cik(self, cik: int) -> tuple[str, str | None] | None:
        hits = self._by_cik.get(int(cik))
        return hits[0] if hits else None

    def tickers_for_cik(self, cik: int) -> list[tuple[str, str | None]]:
        """Every (ticker, exchange) listed under `cik`, in file order."""
        return list(self._by_cik.get(int(cik), ()))

    def cik_for_ticker(self, ticker: str) -> int | None:
        hit = self._by_ticker.get(normalise(ticker))
        return hit[0] if hit else None

    def ambiguous(self, ticker: str) -> bool:
        """True when `ticker` is listed under more than one CIK."""
        return normalise(ticker) in self._ambiguous

    def __len__(self) -> int:
        return len(self._by_ticker)

    # ------------------------------------------------------------------ refresh

    def _set(self, rows: list[tuple[str, int, str | None]]) -> None:
        by_ticker: dict[str, tuple[int, str | None]] = {}
        by_cik: dict[int, list[tuple[str, str | None]]] = {}
        ciks: dict[str, set[int]] = {}
        for ticker, cik, exchange in rows:
            by_ticker.setdefault(ticker, (cik, exchange))  # the first row of a ticker answers
            ciks.setdefault(ticker, set()).add(cik)
            listed = by_cik.setdefault(cik, [])
            if all(t != ticker for t, _ in listed):
                listed.append((ticker, exchange))
        self._by_ticker, self._by_cik = by_ticker, by_cik
        self._ambiguous = frozenset(t for t, c in ciks.items() if len(c) > 1)

    def due(self, now: datetime) -> bool:
        """At start; then once per ET day from 03:50 ET; a failed read again after
        `ticker_map_retry_s`."""
        retry_s = float(getattr(self.cfg, "ticker_map_retry_s", RETRY_S))
        waited = self._last_try is None or (now - self._last_try).total_seconds() >= retry_s
        if not self.ready() or self.from_store:
            return waited
        today = et_date(now)
        if self.loaded_for == today or now < et_datetime(today, REFRESH_AT_ET):
            return False
        return waited or et_date(self._last_try) != today  # type: ignore[arg-type]

    def ensure(self, now: datetime) -> bool:
        """Refresh when due; returns True when a read was made."""
        if not self.due(now):
            return False
        self.refresh(now)
        return True

    def refresh(self, now: datetime) -> bool:
        """One read of the map. True when a fresh map is loaded. Never raises."""
        self._last_try = now
        try:
            return self._refresh(now)
        except Exception as exc:  # noqa: BLE001 - a reference read never stops Scout
            self.last_error = f"unexpected {type(exc).__name__}"
            log.warning("edgar_tickers: refresh failed (%s)", type(exc).__name__)
            self._publish("stale", now)
            return False

    def _refresh(self, now: datetime) -> bool:
        src = self.cfg.source(TICKERS)
        if not src.enabled:
            self._publish("disabled", now)
            self._fallback()
            return False
        fetch = self.fetcher.fetch(TICKERS, src.url)
        if fetch.result == "not_modified" and self.ready():
            self._record(src, fetch, now)
            self.loaded_for, self.from_store, self.last_ok = et_date(now), False, fetch.ended_at
            self._publish("ok", now)
            return True
        if fetch.result != "ok":
            self.last_error = fetch.error or fetch.result
            self._record(src, fetch, now)
            self._fallback()
            self._publish("stale" if fetch.result != "no_http" else "unavailable", now)
            return False
        try:
            rows = parse_ticker_rows(parse_json(fetch.body or b""))
            if not rows:
                raise SchemaError("company_tickers_exchange: no rows read")
        except SchemaError as exc:
            self.last_error = f"schema: {exc}"
            self._record(src, fetch, now, result="schema", error=str(exc))
            self._fallback()
            self._publish("unavailable", now)
            return False
        self.raw.save(TICKERS, RAW_NAME, fetch.body or b"", now)
        self._set(rows)
        self.loaded_for, self.from_store, self.last_ok = et_date(now), False, fetch.ended_at
        self._record(src, fetch, now, items=len(self._by_ticker))
        self._publish("ok", now)
        log.info("edgar_tickers: %d tickers loaded", len(self._by_ticker))
        return True

    def _fallback(self) -> None:
        """Use the last stored copy when nothing is in memory."""
        if self.ready():
            return
        latest = getattr(self.raw, "latest", None)
        body = latest(TICKERS, RAW_NAME) if callable(latest) else None
        if not body:
            return
        try:
            rows = parse_ticker_rows(parse_json(body))
        except SchemaError:
            return
        if rows:
            self._set(rows)
            self.from_store = True
            log.info("edgar_tickers: using the last stored map (%d tickers)", len(self._by_ticker))

    def _record(self, src: Any, fetch: FetchResult, now: datetime, *, result: str | None = None,
                error: str | None = None, items: int = 0) -> None:
        if self.health is None:
            return
        self.health.record(self.tracker.observe(src, "tickers", fetch, now=now, result=result,
                                                guids=[f"map:{et_date(now)}"] if items else (), new_items=items,
                                                error=error))

    def _publish(self, status: str, now: datetime) -> None:
        if self.board is None:
            return
        src = self.cfg.source(TICKERS)
        self.board.update(TICKERS, board_state(src.enabled, status, self.last_ok, self.last_error, False))


class SubmissionsClient:
    """implements SubmissionsSource."""

    def __init__(self, cfg: ScoutConfig, fetcher: Fetcher, ticker_map: TickerMap, clock: Clock, *,
                 raw: RawSink | None = None, health: HealthSink | None = None,
                 tracker: CoverageTracker | None = None) -> None:
        self.cfg = cfg
        self.fetcher, self.ticker_map, self.clock = fetcher, ticker_map, clock
        self.raw, self.health = raw, health
        self.tracker = tracker or CoverageTracker(cfg)
        self._cache: dict[tuple[int, date], PeriodicInfo] = {}

    def periodic_info(self, ticker: str, now: datetime) -> PeriodicInfo:
        symbol = normalise(str(ticker or ""))
        try:
            return self._periodic_info(symbol, now)
        except Exception as exc:  # noqa: BLE001 - never raises
            log.warning("edgar_submissions %s: %s", symbol, type(exc).__name__)
            return PeriodicInfo(ticker=symbol, cik=None, status="fetch_failed")

    def _periodic_info(self, symbol: str, now: datetime) -> PeriodicInfo:
        if not self.cfg.http_allowed():
            return PeriodicInfo(ticker=symbol, cik=None, status="no_http")
        src = self.cfg.source(SUBMISSIONS)
        if not src.enabled:
            return PeriodicInfo(ticker=symbol, cik=None, status="fetch_failed")
        cik = self.ticker_map.cik_for_ticker(symbol) if symbol else None
        ambiguous = getattr(self.ticker_map, "ambiguous", None)
        if cik is not None and callable(ambiguous) and ambiguous(symbol):
            return PeriodicInfo(ticker=symbol, cik=None, status="ambiguous_ticker")
        if cik is None:
            ready = getattr(self.ticker_map, "ready", None)
            loaded = bool(ready()) if callable(ready) else True
            return PeriodicInfo(ticker=symbol, cik=None, status="no_cik" if loaded else "fetch_failed")
        day = et_date(now)
        cached = self._cache.get((cik, day))
        if cached is not None:
            return PeriodicInfo(ticker=symbol, cik=cik, status=cached.status, filings=cached.filings,
                                fiscal_year_end=cached.fiscal_year_end, fetched_at=cached.fetched_at)
        fetch = self.fetcher.fetch(SUBMISSIONS, src.url.format(cik=cik), conditional=False)
        if fetch.status == 404:
            info = PeriodicInfo(ticker=symbol, cik=cik, status="no_cik", fetched_at=fetch.ended_at)
            self._remember(cik, day, info)
            self._record(src, fetch, now)
            return info
        if fetch.result != "ok":
            self._record(src, fetch, now)
            status = "no_http" if fetch.result == "no_http" else "fetch_failed"
            return PeriodicInfo(ticker=symbol, cik=cik, status=status)
        try:
            filings, fye = parse_periodic(fetch.body or b"")
        except SchemaError as exc:
            self._record(src, fetch, now, result="schema", error=str(exc))
            info = PeriodicInfo(ticker=symbol, cik=cik, status="schema", fetched_at=fetch.ended_at)
            self._remember(cik, day, info)
            return info
        if self.raw is not None:
            self.raw.save(SUBMISSIONS, f"CIK{cik:010d}.json", fetch.body or b"", now)
        info = PeriodicInfo(ticker=symbol, cik=cik, status="ok", filings=tuple(filings), fiscal_year_end=fye,
                            fetched_at=fetch.ended_at)
        self._remember(cik, day, info)
        self._record(src, fetch, now, items=len(filings))
        return info

    def _remember(self, cik: int, day: date, info: PeriodicInfo) -> None:
        self._cache = {k: v for k, v in self._cache.items() if k[1] >= day - timedelta(days=1)}
        self._cache[(cik, day)] = info

    def _record(self, src: Any, fetch: FetchResult, now: datetime, *, result: str | None = None,
                error: str | None = None, items: int = 0) -> None:
        if self.health is None:
            return
        self.health.record(self.tracker.observe(src, "submissions", fetch, now=now, result=result,
                                                guids=[fetch.url] if items else (), new_items=items, error=error))


def build_ticker_map(ctx: Any) -> TickerMapClient:
    return TickerMapClient(ctx.cfg, ctx.fetcher(), ctx.raw(), ctx.clock, health=ctx.health(), board=ctx.board)


def build_submissions(ctx: Any) -> SubmissionsClient:
    return SubmissionsClient(ctx.cfg, ctx.fetcher(), ctx.ticker_map(), ctx.clock, raw=ctx.raw(),
                             health=ctx.health())
