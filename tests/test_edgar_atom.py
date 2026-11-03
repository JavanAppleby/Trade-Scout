"""scout.edgar_atom (SCOUT_SPEC 4.1 #1, 6.2-6.4; DESIGN 7, D6): one feed per form, filing facts,
coverage, schema drift -> unavailable with no facts."""

from __future__ import annotations

from datetime import timedelta

from helpers_fakes import FakeBoard, FakeFactSink, FakeFetcher, FakeHealthSink, FakeRawSink, FakeTickerMap, fetch_result
from helpers_wire import FORMS, atom_fixture_for, atom_url, fixture, wire_config

from scout.edgar_atom import EdgarAtomPoller, form_url
from scout.facts import make_fact_id

MAP = {1234567: ("ACME", "Nasdaq"), 7654321: ("BETA", "NYSE"), 1111111: ("GAMA", "OTC"), 1652044: ("GOOGL", "Nasdaq")}


def setup(scout_dir, clock, results=None, ticker_map=None, **cfg_kw):
    cfg = wire_config(scout_dir, **cfg_kw)
    if results is None:
        results = {atom_url(cfg, f): fixture(atom_fixture_for(f)) for f in cfg.edgar_atom_forms}
    fetcher, raw, facts, health, board = FakeFetcher(results), FakeRawSink(), FakeFactSink(), FakeHealthSink(), FakeBoard()
    poller = EdgarAtomPoller(cfg, fetcher, raw, facts, health, board, ticker_map or FakeTickerMap(MAP), clock)
    return cfg, poller, fetcher, raw, facts, health, board


def test_one_request_per_form_and_url_quoting(scout_dir, clock):
    cfg, poller, fetcher, *_ = setup(scout_dir, clock)
    poller.poll(clock.now())
    urls = [u for _, u in fetcher.calls]
    assert len(urls) == len(FORMS) and all(s == "edgar_atom" for s, _ in fetcher.calls)
    assert "type=NT%2010-K&" in urls[2] and "type=8-K&" in urls[0] and "count=100" in urls[0]
    assert form_url("x?type={form}&y", "424B") == "x?type=424B&y"


def test_filing_facts(scout_dir, clock):
    cfg, poller, _, raw, facts, health, board = setup(scout_dir, clock)
    added = poller.poll(clock.now())
    assert added == 9 and len(facts.facts) == 9  # 6 8-K entries + 3 424B entries
    by = {(f.ticker, f.form, f.accession): f for f in facts.facts}
    acme = by[("ACME", "8-K", "0001234567-26-000101")]
    assert acme.kind == "filing" and acme.cls == "earnings_result" and acme.items == ("2.02", "9.01")
    assert acme.cik == 1234567 and acme.exchange == "Nasdaq" and acme.feed == "8-K" and acme.source == "edgar_atom"
    assert acme.published_at == "2026-11-02T13:31:12Z" and acme.first_seen_at == "2026-11-02T14:00:00Z"
    assert acme.fact_id == make_fact_id("edgar_atom", "0001234567-26-000101|1234567", "ACME")
    assert acme.raw_sha256 and acme.extra["company"] == "ACME CORP"
    assert by[("BETA", "8-K", "0007654321-26-000044")].cls == "offering"
    assert by[("GAMA", "8-K", "0001111111-26-000009")].cls == "listing_notice"
    assert by[("GOOGL", "8-K", "0001193125-26-000777")].cls == "contract_or_other"
    unknown = by[(None, "8-K", "0009999999-26-000001")]
    assert unknown.cik == 9999999 and unknown.exchange is None
    assert by[("ACME", "8-K/A", "0001234567-26-000099")].cls is None  # 5.03 without "reverse"
    assert {f.cls for f in facts.facts if f.feed == "424B"} == {"offering"}
    assert len(raw.saved) == 9 and all(s[0] == "edgar_atom" for s in raw.saved)
    assert [h.feed for h in health.records] == FORMS
    assert board.sources["edgar_atom"].status == "ok" and board.sources["edgar_atom"].last_ok


def test_second_poll_adds_nothing_and_overlaps(scout_dir, clock):
    cfg, poller, _, raw, facts, health, board = setup(scout_dir, clock)
    poller.poll(clock.now())
    clock.advance(60)
    assert poller.poll(clock.now()) == 0 and len(raw.saved) == 9
    second = health.records[len(FORMS):]
    assert all(h.overlap is True and h.gap_before is False for h in second)
    assert board.sources["edgar_atom"].gap_open is False


def test_schema_drift_refuses_the_source_with_no_facts(scout_dir, clock):
    cfg = wire_config(scout_dir)
    results = {atom_url(cfg, f): fixture(atom_fixture_for(f)) for f in FORMS}
    results[atom_url(cfg, "8-K")] = b"<html>We changed our website</html>"
    _, poller, _, _, facts, health, board = setup(scout_dir, clock, results)
    poller.poll(clock.now())
    assert all(f.feed != "8-K" for f in facts.facts)
    eight_k = [h for h in health.records if h.feed == "8-K"][0]
    assert eight_k.result == "schema" and eight_k.gap_before is True and eight_k.items == 0
    assert board.sources["edgar_atom"].status == "unavailable" and board.sources["edgar_atom"].gap_open is True
    assert "schema" in board.sources["edgar_atom"].last_error


def test_failed_fetch_is_a_stale_source(scout_dir, clock):
    cfg = wire_config(scout_dir)
    results = {atom_url(cfg, "8-K"): fixture(atom_fixture_for("8-K")),
               atom_url(cfg, "424B"): fetch_result("edgar_atom", atom_url(cfg, "424B"), None, result="timeout",
                                                   status=None)}
    _, poller, _, _, _, health, board = setup(scout_dir, clock, results)
    poller.poll(clock.now())
    assert [h.result for h in health.records] == ["ok", "timeout", "network"]
    assert board.sources["edgar_atom"].status == "stale" and board.sources["edgar_atom"].last_ok is None


def test_not_modified(scout_dir, clock):
    cfg = wire_config(scout_dir)
    results = {atom_url(cfg, f): fetch_result("edgar_atom", atom_url(cfg, f), None, result="not_modified", status=304)
               for f in FORMS}
    _, poller, _, _, facts, health, board = setup(scout_dir, clock, results)
    assert poller.poll(clock.now()) == 0 and facts.facts == []
    assert all(h.result == "not_modified" and h.overlap for h in health.records)
    assert board.sources["edgar_atom"].status == "ok"


class NotReady(FakeTickerMap):
    def ready(self) -> bool:
        return False


def test_no_poll_until_the_ticker_map_is_loaded(scout_dir, clock):
    _, poller, fetcher, _, facts, health, board = setup(scout_dir, clock, ticker_map=NotReady(MAP))
    assert poller.poll(clock.now()) == 0
    assert fetcher.calls == [] and health.records == [] and facts.facts == []
    assert board.sources["edgar_atom"].status == "stale" and board.sources["edgar_atom"].gap_open is True


def test_disabled_source(scout_dir, clock):
    _, poller, fetcher, *_rest, board = setup(scout_dir, clock, sources={"edgar_atom": {"enabled": False}})
    assert poller.poll(clock.now()) == 0 and fetcher.calls == []
    assert board.sources["edgar_atom"].status == "disabled"


def test_a_failed_fact_write_is_retried_next_poll(scout_dir, clock):
    class Flaky(FakeFactSink):
        fail = True

        def add(self, fact):
            if self.fail:
                raise OSError("disk full")
            return super().add(fact)

    cfg, poller, _, _, _, health, _ = setup(scout_dir, clock)
    flaky = Flaky()
    poller.facts = flaky
    try:
        poller.poll(clock.now())
    except OSError:
        pass
    # no record claims a poll whose facts were not stored (a form with nothing new still records)
    assert all(r.items == 0 for r in health.records)
    flaky.fail = False
    clock.advance(timedelta(seconds=60).total_seconds())
    assert poller.poll(clock.now()) == 9


def test_first_seen_is_when_the_page_arrived_never_earlier(scout_dir, clock):
    """No look-ahead (SS 2.9): a page fetched after a spacing wait is first seen at its arrival."""
    cfg = wire_config(scout_dir)
    later = clock.now() + timedelta(seconds=5)
    results = {atom_url(cfg, "8-K"): fetch_result("edgar_atom", atom_url(cfg, "8-K"), fixture(atom_fixture_for("8-K")),
                                                  at=later)}
    _, poller, _, _, facts, _, _ = setup(scout_dir, clock, results)
    poller.poll(clock.now())
    assert facts.facts and {f.first_seen_at for f in facts.facts} == {"2026-11-02T14:00:05Z"}


def test_one_form_that_raises_never_stops_the_later_forms(scout_dir, clock):
    """Regression (coverage finding): an exception in an earlier form's store aborted the whole
    poll, so later forms stopped writing health records and could drop out of coverage."""
    class FirstFormFails(FakeFactSink):
        def add(self, fact):
            if fact.feed == FORMS[0]:
                raise OSError("disk full")
            return super().add(fact)

    cfg, poller, fetcher, _, _, health, _ = setup(scout_dir, clock)
    poller.facts = FirstFormFails()
    poller.poll(clock.now())
    assert len(fetcher.calls) == len(FORMS)
    assert {r.feed for r in health.records} == set(FORMS[1:])


def test_a_filing_becomes_one_fact_per_ticker_under_its_cik(scout_dir, clock, paths):
    """Regression: only the CIK's first ticker got the filing fact, so GOOG (after GOOGL) or a
    warrant line never saw its company's 8-K, and verify could not count a gate-5 miss on it."""
    from scout.edgar_submissions import TickerMapClient
    from scout.raw import RawStore

    cfg = wire_config(scout_dir)
    tmap = TickerMapClient(cfg, FakeFetcher({cfg.source("edgar_tickers").url: fixture("company_tickers_exchange_excerpt.json")}),
                           RawStore(paths), clock)
    assert tmap.refresh(clock.now())
    _, poller, _, raw, facts, *_ = setup(scout_dir, clock, ticker_map=tmap)
    poller.poll(clock.now())
    goog = [f for f in facts.facts if f.accession == "0001193125-26-000777"]
    assert sorted(f.ticker for f in goog) == ["GOOG", "GOOGL"]
    assert len({f.fact_id for f in goog}) == 2 and goog[0].guid == goog[1].guid
    acme = {f.ticker for f in facts.facts if f.accession == "0001234567-26-000101"}
    assert acme == {"ACME", "ACMEW"}
    assert len(raw.saved) == 9  # the raw body is still kept once per filing


def test_an_8k_amendment_is_not_a_fresh_catalyst():
    """Regression: an 8-K/A classed as its base form, so the financial statements for an acquisition
    re-filed ~10 weeks later (items 2.01, 9.01) made gate 6 true on stale news."""
    from scout.classify import filing_class

    assert filing_class("8-K/A", ("2.01", "9.01")) is None
    assert filing_class("8-K/A", ("8.01",)) is None and filing_class("8-K/A", ("1.01",)) is None
    assert filing_class("8-K/A", ("2.02",)) is None
    assert filing_class("8-K/A", ("3.02",)) == "offering"  # blocking classes are kept (the safe way)
    assert filing_class("8-K/A", ("3.01",)) == "listing_notice"
    assert filing_class("8-K/A", ("5.03",), "reverse stock split") == "reverse_split"
    assert filing_class("8-K", ("2.01", "9.01")) == "contract_or_other"
