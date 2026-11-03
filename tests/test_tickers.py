"""scout.tickers (SCOUT_SPEC 6.3): exchange tags -> one (exchange, ticker) per name."""

from __future__ import annotations

import pytest

from scout.config import ScoutConfig
from scout.tickers import normalise, tickers_in, title_suffix_ticker

PREFIXES = ScoutConfig().ticker_exchange_prefixes


@pytest.mark.parametrize("text, expected", [
    ("Acme (NASDAQ: ACME) today", [("NASDAQ", "ACME")]),
    ("(NASDAQ:ACME)", [("NASDAQ", "ACME")]),
    ("(Nasdaq: ACME)", [("Nasdaq", "ACME")]),
    ("(nasdaq: ACME)", [("NASDAQ", "ACME")]),
    ("(NYSE American: ZETA)", [("NYSE American", "ZETA")]),
    ("(NYSE  American : ZETA)", [("NYSE American", "ZETA")]),
    ("(NYSE: BETA)", [("NYSE", "BETA")]),
    ("(NYSE Arca: XYZ)", [("NYSE Arca", "XYZ")]),
    ("(OTCQB: GAMA)", [("OTCQB", "GAMA")]),
    ("(OTCQX: XILG)", [("OTCQX", "XILG")]),
    ("(OTC: OTCX)", [("OTC", "OTCX")]),
    ("(Cboe BZX: TAUI)", [("Cboe BZX", "TAUI")]),
    ("(NASDAQ: GOOGL, GOOG)", [("NASDAQ", "GOOGL"), ("NASDAQ", "GOOG")]),
    ("(NYSE: LMDA) (TSX: LMDA)", [("NYSE", "LMDA")]),
    # an unknown exchange is skipped, not the end of the tag (regression: dual listings, TSX first)
    ("(TSX: ABC; NYSE: ABC)", [("NYSE", "ABC")]),
    ("(TSX: ACM; NASDAQ: ACME)", [("NASDAQ", "ACME")]),
    ("(TSX: ACM, ACMX; NASDAQ: ACME)", [("NASDAQ", "ACME")]),  # ACMX inherits TSX: skipped
    ("(NASDAQ: ACME; TSX: ACM, ACMX)", [("NASDAQ", "ACME")]),  # TSX clears the inherited NASDAQ
    ("(Nasdaq: ACME and ACMEW)", [("Nasdaq", "ACME"), ("Nasdaq", "ACMEW")]),
    ("(NASDAQ: ACME & ACMEW)", [("NASDAQ", "ACME"), ("NASDAQ", "ACMEW")]),
    ('(NASDAQ: <a href="https://x.example/ACME" rel="nofollow">ACME</a>)', [("NASDAQ", "ACME")]),
    ("(the &quot;Company&quot;) (NASDAQ: ACME)", [("NASDAQ", "ACME")]),
    ("(NYSE: BRK.B)", [("NYSE", "BRK-B")]),
    ("(NYSE: BF/A)", [("NYSE", "BF-A")]),
    ("(NASDAQ: ACME) and again (NASDAQ: ACME)", [("NASDAQ", "ACME")]),
    ("(NASDAQ: ACME) (NYSE: BETA)", [("NASDAQ", "ACME"), ("NYSE", "BETA")]),
    ("(LSE: ZZZ)", []),
    ("(the company)", []),
    ("NASDAQ: ACME without brackets", []),
    ("(NASDAQ: TOOLONGSYM)", []),
    ("", []),
])
def test_tickers_in(text, expected):
    assert tickers_in(text, PREFIXES) == expected


def test_prefix_list_is_config():
    assert tickers_in("(NASDAQ: ACME) (TSX: ABC)", ["TSX"]) == [("TSX", "ABC")]
    assert tickers_in("(NASDAQ: ACME)", []) == []


@pytest.mark.parametrize("raw, norm", [("brk.b", "BRK-B"), ("BF/A", "BF-A"), (" acme ", "ACME")])
def test_normalise(raw, norm):
    assert normalise(raw) == norm


@pytest.mark.parametrize("text, expected", [
    ("(Nasdaq CM: ACME)", [("Nasdaq CM", "ACME")]),
    ("(NasdaqCM: ACME)", [("NasdaqCM", "ACME")]),
    ("(NASDAQ Capital Market: ACME)", [("NASDAQ Capital Market", "ACME")]),
    ("(Nasdaq Global Market: ACME)", [("Nasdaq Global Market", "ACME")]),
    ("(OTC Pink: ACME)", [("OTC Pink", "ACME")]),
    ("(CSE: ACM / OTCQB: ACMEF)", [("OTCQB", "ACMEF")]),
    ('(the "Company" or "Acme", NASDAQ: ACME)', [("NASDAQ", "ACME")]),
    ("Acme, Inc. (Nasdaq - ACME)", [("Nasdaq", "ACME")]),
    ("Acme, Inc. (Nasdaq \u2013 ACME)", [("Nasdaq", "ACME")]),
    ("(a Nasdaq-listed company)", []),  # a dash needs a space each side
    ("(NYSE: BF/A)", [("NYSE", "BF-A")]),  # "/" inside a symbol is not a split
])
def test_common_exchange_tag_spellings(text, expected):
    """Regression (third review): these spellings gave no ticker, so the fact was stored with
    ticker None and a date notice never reached gate 5."""
    assert tickers_in(text, PREFIXES) == expected


@pytest.mark.parametrize("title, expected", [
    ("Acme Prices Offering | ACME Stock News", "ACME"),
    ("Acme Prices Offering | ACME  Stock  News  ", "ACME"),
    ("Harbor &amp; Pine Declares Dividend | HPHX.B Stock News", "HPHX-B"),
    ("Latest News | Stock Titan", None),
    ("Acme | acme Stock News", None),  # a lower-case word is not a ticker
    ("Acme Stock News", None),  # no "|"
    ("Acme | ACME Stock News today", None),  # the suffix must end the title
    ("Acme | ACMEXYZQ Stock News", None),  # longer than a ticker
])
def test_title_suffix_ticker(title, expected):
    assert title_suffix_ticker(title, ("Stock News",)) == expected


def test_title_suffix_list_is_config():
    assert title_suffix_ticker("Acme | ACME Stock News", ()) is None
    assert title_suffix_ticker("Acme | ACME Company News", ("Stock News", "Company News")) == "ACME"
