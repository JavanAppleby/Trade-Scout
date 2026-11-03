"""scout.wire_rss (SCOUT_SPEC 4.1 #4, 6.1-6.4): RSS parsing, one fact per ticker, titles/links/times
only while terms_ok is false, overlap, schema drift -> unavailable, refused after repeated 403s."""

from __future__ import annotations

from datetime import timedelta

import pytest
from helpers_fakes import FakeBoard, FakeFactSink, FakeFetcher, FakeHealthSink, FakeRawSink, fetch_result
from helpers_wire import TEST_WIRE, fixture, wire_config

from scout.edgar_parse import SchemaError
from scout.facts import make_fact_id
from scout.wire_rss import WirePoller, item_tickers, parse_rss

PRN_ITEMS = parse_rss(fixture("prnewswire.rss"))


# --------------------------------------------------------------------------- parsing


def test_parse_prnewswire_fixture():
    assert len(PRN_ITEMS) == 20  # the feed holds 20 items (SS 6.2)
    first = PRN_ITEMS[0]
    assert first.title == "Acme Corp Reports Third Quarter 2026 Results"
    assert first.guid == first.link and first.link.startswith("https://www.prnewswire.com/news-releases/")
    assert first.published_at == "2026-11-02T13:30:00Z" and first.pub_date == "Mon, 02 Nov 2026 13:30:00 +0000"
    assert "(NASDAQ: ACME)" in first.description


def test_parse_category_fixture():
    items = parse_rss(fixture("category_wire.rss"))
    assert [i.published_at for i in items] == ["2026-11-02T13:05:00Z", "2026-11-02T12:45:00Z", "2026-11-02T12:30:00Z"]
    assert items[0].raw_categories == ("Company Announcement", "Nasdaq:OMGB")


def item(guid="g", title="T", pub="Mon, 02 Nov 2026 13:30:00 +0000", extra="") -> str:
    parts = f"<title>{title}</title>" if title is not None else ""
    parts += f"<guid>{guid}</guid>" if guid is not None else ""
    parts += f"<pubDate>{pub}</pubDate>" if pub is not None else ""
    return f"<item>{parts}{extra}</item>"


def rss(*items: str) -> bytes:
    return f"<rss version=\"2.0\"><channel><title>x</title>{''.join(items)}</channel></rss>".encode()


def test_pubdate_variants():
    got = parse_rss(rss(item("a", pub="Mon, 02 Nov 2026 08:30:00 -0500"), item("b", pub="Mon, 02 Nov 2026 13:05 GMT"),
                        item("c", pub="Mon, 02 Nov 2026 13:30:00 -0000")))
    assert [i.published_at for i in got] == ["2026-11-02T13:30:00Z", "2026-11-02T13:05:00Z", "2026-11-02T13:30:00Z"]
    assert parse_rss(rss()) == []
    link_only = parse_rss(rss(item(None, extra="<link>https://x/1</link>")))
    assert link_only[0].guid == "https://x/1"


@pytest.mark.parametrize("body", [
    rss(item(title=None)), rss(item(guid=None)), rss(item(pub=None)), rss(item(pub="yesterday")),
    rss(item(pub="Mon, 99 Nov 2026 13:30:00 +0000")),
    b"<feed/>", b"<rss><nochannel/></rss>", b"<html>blocked</html>", b"", b"<rss",
    b'<!DOCTYPE rss [<!ENTITY x "y">]><rss><channel/></rss>',
])
def test_schema_drift(body):
    with pytest.raises(SchemaError):
        parse_rss(body)


def test_tickers_from_title_description_and_categories():
    from scout.config import ScoutConfig

    pre = ScoutConfig().ticker_exchange_prefixes
    by_title = {i.title.split()[0]: item_tickers(i, pre) for i in PRN_ITEMS}
    assert by_title["Alphabet"] == [("NASDAQ", "GOOGL"), ("NASDAQ", "GOOG")]
    assert by_title["Lambda"] == [("NYSE", "LMDA")]
    assert by_title["Iota"] == [] and by_title["Tau"] == [("Cboe BZX", "TAUI")]
    gnw = parse_rss(fixture("category_wire.rss"))
    assert item_tickers(gnw[1], pre) == [("NYSE", "UPSN")]


# --------------------------------------------------------------------------- polling


def setup(scout_dir, clock, source="prnewswire", results=None, **cfg_kw):
    cfg = wire_config(scout_dir, **cfg_kw)
    url = cfg.source(source).url
    if results is None:
        results = {url: fixture("category_wire.rss" if source == "testwire" else f"{source}.rss")}
    fetcher, raw, facts, health, board = FakeFetcher(results), FakeRawSink(), FakeFactSink(), FakeHealthSink(), FakeBoard()
    poller = WirePoller(cfg, source, fetcher, raw, facts, health, board, clock)
    return cfg, url, poller, fetcher, raw, facts, health, board


def test_poll_writes_one_fact_per_ticker(scout_dir, clock):
    cfg, url, poller, fetcher, raw, facts, health, board = setup(scout_dir, clock)
    added = poller.poll(clock.now())
    assert added == 21 and len(facts.facts) == 21  # 20 items, Alphabet names two tickers
    goog = [f for f in facts.facts if f.ticker in ("GOOGL", "GOOG")]
    assert len(goog) == 2 and goog[0].guid == goog[1].guid and goog[0].fact_id != goog[1].fact_id
    assert goog[0].fact_id == make_fact_id("prnewswire", goog[0].guid, "GOOGL")
    acme = next(f for f in facts.facts if f.ticker == "ACME")
    assert acme.kind == "wire" and acme.cls == "earnings_result" and acme.exchange == "NASDAQ"
    assert acme.published_at == "2026-11-02T13:30:00Z" and acme.first_seen_at == "2026-11-02T14:00:00Z"
    assert acme.source == "prnewswire" and acme.feed == "rss" and acme.cik is None and acme.raw_sha256
    no_ticker = [f for f in facts.facts if f.ticker is None]
    assert len(no_ticker) == 4 and all(f.exchange is None for f in no_ticker)
    classes = {f.ticker: f.cls for f in facts.facts}
    assert classes["BETA"] == "offering" and classes["THM"] == "reverse_split" and classes["GAMA"] == "listing_notice"
    assert classes["DLTB"] == "fda" and classes["EPSP"] == "trial" and classes["ZETA"] == "contract"
    assert classes["ETAE"] == "earnings_date" and classes["PISW"] == "earnings_date"
    h = health.records[0]
    assert (h.result, h.items, h.new_items, h.overlap, h.gap_before) == ("ok", 20, 20, False, True)
    assert board.sources["prnewswire"].status == "ok" and board.sources["prnewswire"].gap_open is True


def test_titles_links_and_times_only_until_terms_ok(scout_dir, clock):
    cfg, url, poller, fetcher, raw, facts, health, board = setup(scout_dir, clock)
    poller.poll(clock.now())
    bodies = [s[2] for s in raw.saved]
    assert len(bodies) == 20
    assert all(b"/PRNewswire/" not in b and b"<description" not in b and b"<guid>" in b for b in bodies)
    assert all("PRNewswire" not in (f.title or "") for f in facts.facts)
    _, _, ok_poller, _, ok_raw, *_ = setup(scout_dir, clock, sources={"prnewswire": {"terms_ok": True}})
    ok_poller.poll(clock.now())
    assert any(b"<description" in s[2] for s in ok_raw.saved)


def test_second_poll_overlaps_and_adds_only_new_items(scout_dir, clock):
    cfg, url, poller, fetcher, raw, facts, health, board = setup(scout_dir, clock)
    poller.poll(clock.now())
    body = fixture("prnewswire.rss").replace(b"Acme Corp Reports Third", b"Acme Corp Restates Third")
    body = body.replace(b"30290" + b"0000.html", b"302999999.html")
    fetcher.results[url] = body
    clock.advance(60)
    assert poller.poll(clock.now()) == 1
    h = health.records[1]
    assert (h.overlap, h.gap_before, h.new_items) == (True, False, 1)
    assert board.sources["prnewswire"].gap_open is False and len(raw.saved) == 21


def test_schema_drift_makes_the_source_unavailable_with_no_facts(scout_dir, clock):
    cfg, url, poller, fetcher, raw, facts, health, board = setup(scout_dir, clock, results={})
    fetcher.results[url] = fixture("prnewswire.rss").replace(b"<pubDate>Mon, 02 Nov 2026 12:45:00 +0000",
                                                           b"<pubDate>soon", 1)
    assert poller.poll(clock.now()) == 0
    assert facts.facts == [] and raw.saved == []
    assert health.records[0].result == "schema" and board.sources["prnewswire"].status == "unavailable"


def test_failed_poll_is_recorded(scout_dir, clock):
    cfg, url, poller, fetcher, raw, facts, health, board = setup(scout_dir, clock, results={})
    assert poller.poll(clock.now()) == 0
    assert health.records[0].result == "network" and health.records[0].gap_before is True
    assert board.sources["prnewswire"].status == "stale" and board.sources["prnewswire"].last_error


def test_not_modified_poll(scout_dir, clock):
    cfg = wire_config(scout_dir)
    url = cfg.source("prnewswire").url
    nm = fetch_result("prnewswire", url, None, result="not_modified", status=304)
    _, _, poller, fetcher, raw, facts, health, board = setup(scout_dir, clock, results={url: nm})
    assert poller.poll(clock.now()) == 0 and health.records[0].overlap is True
    assert board.sources["prnewswire"].status == "ok"


def test_refused_after_three_403s_for_the_rest_of_the_et_day(scout_dir, clock):
    cfg = wire_config(scout_dir, sources=TEST_WIRE)
    url = cfg.source("testwire").url
    forbidden = fetch_result("testwire", url, b"", result="http_error", status=403)
    _, _, poller, fetcher, _, facts, health, board = setup(scout_dir, clock, "testwire", {url: forbidden},
                                                           sources=TEST_WIRE)
    for _ in range(3):
        poller.poll(clock.now())
        clock.advance(120)
    assert [h.result for h in health.records] == ["http_error", "http_error", "refused"]
    assert board.sources["testwire"].status == "refused"
    calls = len(fetcher.calls)
    clock.advance(3600)
    assert poller.poll(clock.now()) == 0 and len(fetcher.calls) == calls  # not polled again today
    fetcher.results[url] = fixture("category_wire.rss")
    clock.set(clock.now().replace(hour=14) + timedelta(days=1))
    assert poller.poll(clock.now()) == 3 and board.sources["testwire"].status == "ok"


def test_a_success_resets_the_403_count(scout_dir, clock):
    cfg = wire_config(scout_dir, sources=TEST_WIRE)
    url = cfg.source("testwire").url
    forbidden = fetch_result("testwire", url, b"", result="http_error", status=403)
    _, _, poller, fetcher, *_ = setup(scout_dir, clock, "testwire", {url: forbidden}, sources=TEST_WIRE)
    poller.poll(clock.now())
    poller.poll(clock.now())
    assert poller.forbidden_in_a_row == 2  # the 403s were counted (the source is enabled here)
    fetcher.results[url] = fixture("category_wire.rss")
    poller.poll(clock.now())
    assert poller.forbidden_in_a_row == 0 and poller.refused_on is None


def test_stocktitan_ticker_from_the_title_end(scout_dir, clock):
    """Owner, 2026-11-04: Stock Titan names the ticker as "| ZNTV Stock News" with no exchange; an
    exchange tag in the same title wins and the ticker is not stored twice."""
    cfg, url, poller, fetcher, raw, facts, health, board = setup(scout_dir, clock, "stocktitan")
    assert poller.poll(clock.now()) == 4
    assert [(f.ticker, f.exchange, f.cls) for f in facts.facts] == [
        ("ZNTV", None, "offering"), ("QLMK", "NASDAQ", "fda"), ("HPHX-B", None, "other"), (None, None, "other")]
    assert {f.source for f in facts.facts} == {"stocktitan"} and facts.facts[0].title.endswith("| ZNTV Stock News")
    h = health.records[0]
    assert (h.result, h.items, h.new_items) == ("ok", 4, 4) and board.sources["stocktitan"].status == "ok"


def test_title_end_suffix_is_config(scout_dir, clock):
    cfg, url, poller, fetcher, raw, facts, health, board = setup(scout_dir, clock, "stocktitan",
                                                                wire_title_ticker_suffixes=[])
    poller.poll(clock.now())
    assert [f.ticker for f in facts.facts] == [None, "QLMK", None, None]


def test_tickers_from_categories(scout_dir, clock):
    cfg, url, poller, fetcher, raw, facts, health, board = setup(scout_dir, clock, "testwire", sources=TEST_WIRE)
    poller.poll(clock.now())
    assert [(f.ticker, f.exchange, f.cls) for f in facts.facts] == [
        ("OMGB", "Nasdaq", "trial"), ("UPSN", "NYSE", "earnings_result"), ("CHIC", "Nasdaq", "offering")]


def test_disabled_source(scout_dir, clock):
    _, _, poller, fetcher, *_rest, board = setup(scout_dir, clock, sources={"prnewswire": {"enabled": False}})
    assert poller.poll(clock.now()) == 0 and fetcher.calls == []
    assert board.sources["prnewswire"].status == "disabled"


def test_first_seen_is_when_the_feed_arrived(scout_dir, clock):
    cfg = wire_config(scout_dir)
    url = cfg.source("prnewswire").url
    got = fetch_result("prnewswire", url, fixture("prnewswire.rss"), at=clock.now() + timedelta(seconds=3))
    _, _, poller, _, _, facts, _, _ = setup(scout_dir, clock, results={url: got})
    poller.poll(clock.now())
    assert {f.first_seen_at for f in facts.facts} == {"2026-11-02T14:00:03Z"}
