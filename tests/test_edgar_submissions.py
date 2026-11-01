"""scout.edgar_submissions (SCOUT_SPEC 4.1 #2-3; DESIGN 2.2): the CIK map and the submissions client.
periodic_info never raises; cached per CIK per ET day; no HTTP without a contact email."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from helpers_fakes import FakeBoard, FakeFetcher, FakeHealthSink, FakeTickerMap, FakeTransport, fetch_result
from helpers_wire import fixture, wire_config

from scout.edgar_submissions import (
    RAW_NAME, RETRY_S, SubmissionsClient, TickerMapClient, build_submissions, build_ticker_map,
)
from scout.http import HttpFetcher
from scout.raw import RawStore

START = datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc)  # 02:00 ET


def tickers_url(cfg):
    return cfg.source("edgar_tickers").url


def sub_url(cfg, cik):
    return cfg.source("edgar_submissions").url.format(cik=cik)


def map_client(cfg, paths, clock, results):
    fetcher, health, board = FakeFetcher(results), FakeHealthSink(), FakeBoard()
    client = TickerMapClient(cfg, fetcher, RawStore(paths), clock, health=health, board=board)
    return client, fetcher, health, board


# --------------------------------------------------------------------------- ticker map


def test_map_loads_and_answers(scout_dir, paths, clock):
    cfg = wire_config(scout_dir)
    client, fetcher, health, board = map_client(cfg, paths, clock,
                                                {tickers_url(cfg): fixture("company_tickers_exchange_excerpt.json")})
    assert not client.ready() and client.ticker_for_cik(1234567) is None
    assert client.refresh(clock.now()) is True and client.ready() and len(client) == 8
    assert client.ticker_for_cik(1234567) == ("ACME", "Nasdaq")  # the first listing wins over ACMEW
    assert client.ticker_for_cik(1652044) == ("GOOGL", "Nasdaq")
    assert client.ticker_for_cik(3333333) == ("DLTF", None)
    assert client.cik_for_ticker("brk.b") == 1067983 and client.cik_for_ticker("goog") == 1652044
    assert client.cik_for_ticker("NOPE") is None and client.cik_for_ticker("BRKN") is None
    assert board.sources["edgar_tickers"].status == "ok" and health.records[0].result == "ok"
    assert list((paths.raw / "edgar_tickers").rglob(f"*{RAW_NAME}"))  # kept in the raw store too


def test_map_schedule_start_then_daily_at_0350_et(scout_dir, paths):
    from scout.clock import ManualClock

    cfg = wire_config(scout_dir)
    clock = ManualClock(START)
    client, fetcher, _, _ = map_client(cfg, paths, clock,
                                       {tickers_url(cfg): fixture("company_tickers_exchange_excerpt.json")})
    assert client.ensure(clock.now()) is True  # at start
    clock.advance(3600)  # 03:00 ET
    assert client.ensure(clock.now()) is False
    clock.set(datetime(2026, 11, 3, 8, 49, tzinfo=timezone.utc))  # 03:49 ET next day
    assert client.ensure(clock.now()) is False
    clock.set(datetime(2026, 11, 3, 8, 50, tzinfo=timezone.utc))  # 03:50 ET
    assert client.ensure(clock.now()) is True and client.ensure(clock.now()) is False
    assert len(fetcher.calls) == 2


def test_map_failure_retries_after_ten_minutes_and_uses_the_stored_copy(scout_dir, paths, clock):
    cfg = wire_config(scout_dir)
    good = fixture("company_tickers_exchange_excerpt.json")
    RawStore(paths).save("edgar_tickers", RAW_NAME, good, clock.now() - timedelta(days=2))
    client, fetcher, health, board = map_client(cfg, paths, clock, {})  # every fetch fails
    assert client.ensure(clock.now()) is True and client.from_store and client.ready()
    assert client.cik_for_ticker("ACME") == 1234567
    assert board.sources["edgar_tickers"].status == "stale" and health.records[0].result == "network"
    assert client.ensure(clock.now() + timedelta(seconds=RETRY_S - 1)) is False
    fetcher.results[tickers_url(cfg)] = good
    assert client.ensure(clock.now() + timedelta(seconds=RETRY_S)) is True and not client.from_store


@pytest.mark.parametrize("body", [b"<html>", b'{"fields": ["a"], "data": []}', b"{}"])
def test_map_schema_drift_is_unavailable(scout_dir, paths, clock, body):
    cfg = wire_config(scout_dir)
    client, _, health, board = map_client(cfg, paths, clock, {tickers_url(cfg): body})
    assert client.refresh(clock.now()) is False and not client.ready()
    assert health.records[0].result == "schema" and board.sources["edgar_tickers"].status == "unavailable"


def test_map_not_modified_keeps_the_map(scout_dir, paths, clock):
    cfg = wire_config(scout_dir)
    client, fetcher, _, _ = map_client(cfg, paths, clock,
                                       {tickers_url(cfg): fixture("company_tickers_exchange_excerpt.json")})
    client.refresh(clock.now())
    fetcher.results[tickers_url(cfg)] = fetch_result("edgar_tickers", tickers_url(cfg), None,
                                                     result="not_modified", status=304)
    assert client.refresh(clock.now() + timedelta(days=1)) is True and client.ready()


def test_map_refresh_never_raises(scout_dir, paths, clock):
    class Boom:
        def fetch(self, *a, **k):
            raise RuntimeError("x")

    cfg = wire_config(scout_dir)
    client = TickerMapClient(cfg, Boom(), RawStore(paths), clock, board=FakeBoard())
    assert client.refresh(clock.now()) is False


# --------------------------------------------------------------------------- submissions

MAP = FakeTickerMap({1234567: ("ACME", "Nasdaq"), 7654321: ("BETA", "NYSE"), 5555555: ("GONE", None)})


def sub_client(cfg, clock, results, ticker_map=MAP):
    fetcher, health = FakeFetcher(results), FakeHealthSink()
    return SubmissionsClient(cfg, fetcher, ticker_map, clock, health=health), fetcher, health


def test_periodic_info_ok_and_cached_per_day(scout_dir, clock):
    cfg = wire_config(scout_dir)
    client, fetcher, health = sub_client(cfg, clock, {sub_url(cfg, 7654321): fixture("submissions_june_fy_filer.json")})
    info = client.periodic_info("beta", clock.now())
    assert info.status == "ok" and info.ticker == "BETA" and info.cik == 7654321 and info.fiscal_year_end == "0630"
    assert [f.form for f in info.filings][:3] == ["8-K", "10-K", "NT 10-K"]
    assert info.fetched_at == "2026-11-02T14:00:00Z" and health.records[0].result == "ok"
    again = client.periodic_info("BETA", clock.now() + timedelta(hours=5))
    assert again == info and len(fetcher.calls) == 1
    client.periodic_info("BETA", clock.now() + timedelta(days=1))
    assert len(fetcher.calls) == 2


def test_periodic_info_statuses(scout_dir, clock):
    cfg = wire_config(scout_dir)
    results = {sub_url(cfg, 5555555): fetch_result("edgar_submissions", sub_url(cfg, 5555555), b"", result="http_error",
                                                   status=404),
               sub_url(cfg, 1234567): fetch_result("edgar_submissions", sub_url(cfg, 1234567), b"", result="http_error",
                                                   status=500)}
    client, fetcher, _ = sub_client(cfg, clock, results)
    assert client.periodic_info("NOPE", clock.now()).status == "no_cik" and fetcher.calls == []
    assert client.periodic_info("GONE", clock.now()).status == "no_cik"
    failed = client.periodic_info("ACME", clock.now())
    assert failed.status == "fetch_failed" and failed.cik == 1234567
    client.periodic_info("ACME", clock.now())
    assert len([c for c in fetcher.calls if "1234567" in c[1]]) == 2  # a failure is not cached


def test_periodic_info_schema_is_cached_for_the_day(scout_dir, clock):
    cfg = wire_config(scout_dir)
    client, fetcher, health = sub_client(cfg, clock, {sub_url(cfg, 1234567): b'{"filings": {}}'})
    assert client.periodic_info("ACME", clock.now()).status == "schema"
    assert client.periodic_info("ACME", clock.now()).status == "schema" and len(fetcher.calls) == 1
    assert health.records[0].result == "schema"


def test_map_not_loaded_is_fetch_failed_not_no_cik(scout_dir, clock):
    class Empty(FakeTickerMap):
        def ready(self):
            return False

    cfg = wire_config(scout_dir)
    client, fetcher, _ = sub_client(cfg, clock, {}, ticker_map=Empty())
    assert client.periodic_info("ACME", clock.now()).status == "fetch_failed" and fetcher.calls == []


def test_no_contact_email_means_no_http_and_zero_transport_calls(scout_dir, clock, no_network):
    from scout.config import config_from_dict

    cfg = config_from_dict({}, scout_dir)
    transport = FakeTransport({})
    client = SubmissionsClient(cfg, HttpFetcher(cfg, clock, transport=transport), MAP, clock)
    assert client.periodic_info("ACME", clock.now()).status == "no_http"
    assert transport.calls == [] and no_network.attempts == []


def test_periodic_info_never_raises(scout_dir, clock):
    class Boom:
        def cik_for_ticker(self, t):
            raise RuntimeError("x")

    cfg = wire_config(scout_dir)
    client = SubmissionsClient(cfg, FakeFetcher({}), Boom(), clock)  # type: ignore[arg-type]
    assert client.periodic_info("ACME", clock.now()).status == "fetch_failed"
    assert client.periodic_info(None, clock.now()).status in ("no_cik", "fetch_failed")  # type: ignore[arg-type]


def test_end_to_end_through_the_real_fetcher(scout_dir, paths, clock):
    """Real HttpFetcher with a fake transport: User-Agent sent, raw kept, conditional off."""
    from helpers_wire import full_transport

    cfg = wire_config(scout_dir)
    transport = full_transport(cfg)
    http = HttpFetcher(cfg, clock, transport=transport)
    tmap = TickerMapClient(cfg, http, RawStore(paths), clock)
    tmap.refresh(clock.now())
    client = SubmissionsClient(cfg, http, tmap, clock, raw=RawStore(paths))
    info = client.periodic_info("ACME", clock.now())
    assert info.status == "ok" and info.fiscal_year_end == "1231"
    assert all("User-Agent" in c[1] for c in transport.calls)
    assert list((paths.raw / "edgar_submissions").rglob("*CIK0001234567.json"))


def test_builders_use_context_services(scout_dir, paths, clock):
    cfg = wire_config(scout_dir)
    fetcher = FakeFetcher({})
    ctx = SimpleNamespace(cfg=cfg, clock=clock, board=FakeBoard(), fetcher=lambda: fetcher,
                          raw=lambda: RawStore(paths), health=lambda: FakeHealthSink())
    tmap = build_ticker_map(ctx)
    ctx.ticker_map = lambda: tmap
    subs = build_submissions(ctx)
    assert isinstance(tmap, TickerMapClient) and isinstance(subs, SubmissionsClient) and subs.ticker_map is tmap


def test_a_ticker_under_two_ciks_is_ambiguous_and_gets_no_due_window(scout_dir, paths, clock):
    """Regression: the first row of a ticker listed under two CIKs won silently, so the due window
    (gate 5) could be built from the wrong company's 10-Q cycle and answer a confident false."""
    import json

    from scout.due_window import due_window

    body = json.dumps({"fields": ["cik", "name", "ticker", "exchange"], "data": [
        [1000001, "A CORP", "S", "Nasdaq"], [1000002, "S CORP", "S", "Nasdaq"], [7654321, "BETA", "BETA", "NYSE"],
    ]}).encode()
    cfg = wire_config(scout_dir)
    tmap, *_ = map_client(cfg, paths, clock, {tickers_url(cfg): body})
    assert tmap.refresh(clock.now())
    assert tmap.ambiguous("s") and not tmap.ambiguous("BETA")
    fetcher = FakeFetcher({})
    client = SubmissionsClient(cfg, fetcher, tmap, clock)
    info = client.periodic_info("S", clock.now())
    assert info.status == "ambiguous_ticker" and info.cik is None and fetcher.calls == []
    assert due_window(info, cfg, clock.now()).status == "ambiguous_ticker"
    assert tmap.tickers_for_cik(1000002) == [("S", "Nasdaq")]  # the second CIK keeps its ticker


def test_every_ticker_under_a_cik_is_kept_in_file_order(scout_dir, paths, clock):
    cfg = wire_config(scout_dir)
    client, *_ = map_client(cfg, paths, clock, {tickers_url(cfg): fixture("company_tickers_exchange_excerpt.json")})
    client.refresh(clock.now())
    assert client.tickers_for_cik(1234567) == [("ACME", "Nasdaq"), ("ACMEW", "Nasdaq")]
    assert client.tickers_for_cik(1652044) == [("GOOGL", "Nasdaq"), ("GOOG", "Nasdaq")]
    assert client.tickers_for_cik(42) == [] and not client.ambiguous("ACME")
