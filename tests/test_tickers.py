"""scout.tickers (SCOUT_SPEC 6.3): exchange tags -> one (exchange, ticker) per name."""

from __future__ import annotations

import pytest

from scout.config import ScoutConfig
from scout.tickers import normalise, tickers_in

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
    ("(OTC Pink: ZZZ)", []),
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
