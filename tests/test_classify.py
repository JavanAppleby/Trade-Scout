"""scout.classify (SCOUT_SPEC 6.4): wire titles in the fixed order from config keywords; the filing
table."""

from __future__ import annotations

import pytest

from scout.classify import filing_class, keyword_hit, wire_class
from scout.config import ScoutConfig
from scout.models import WIRE_CLASSES

KW = ScoutConfig().class_keywords


@pytest.mark.parametrize("title, cls", [
    ("Beta Announces Pricing of $20 Million Public Offering", "offering"),
    ("Nu Retail Announces Registered Direct Offering", "offering"),
    ("Chi Capital Announces Closing of Private Placement", "offering"),
    ("Theta Mining Announces 1-for-20 Reverse Stock Split", "reverse_split"),
    ("Gamma Receives Nasdaq Notice Regarding Minimum Bid Price", "listing_notice"),
    ("Delta Bio Announces FDA Approval of Lead Candidate", "fda"),
    ("Mu Devices Receives FDA 510(k) Clearance", "fda"),
    ("Epsilon Reports Positive Topline Phase 2 Data", "trial"),
    ("Zeta Systems Awarded $45 Million Contract by U.S. Army", "contract"),
    ("Tau Industries Receives Purchase Order from Major Retailer", "contract"),
    ("Acme Corp Reports Third Quarter 2026 Results", "earnings_result"),
    ("Rho Health Reports Financial Results for Third Quarter", "earnings_result"),
    ("Eta Energy to Report Third Quarter 2026 Results on November 10", "earnings_date"),
    ("Pi Software Will Announce Q3 Results and Host Conference Call", "earnings_date"),
    ("Kappa Charity Gala Raises Record Funds", "other"),
    ("Reorder of shelves", "other"),  # whole words only: "order" does not match "Reorder"
    # first match in the fixed order wins: an offering tied to a contract is an offering
    ("Acme Prices Public Offering to Fund New Contract", "offering"),
    ("Acme Announces FDA Approval and Reverse Split", "reverse_split"),
])
def test_wire_class(title, cls):
    assert wire_class(title, KW) == cls


def test_keywords_are_config_and_case_insensitive():
    custom = {c: [] for c in WIRE_CLASSES if c != "other"}
    custom["fda"] = ["widget"]
    assert wire_class("New WIDGET launch", custom) == "fda"
    assert wire_class("Pricing of offering", custom) == "other"
    assert keyword_hit("multi   word  phrase here", ["word phrase"]) == "word phrase"
    assert keyword_hit("x", ["", "  "]) is None


@pytest.mark.parametrize("form, items, desc, cls", [
    ("8-K", ["1.01"], "", "contract_or_other"),
    ("8-K", ["2.01", "9.01"], "", "contract_or_other"),
    ("8-K", ["7.01"], "", "contract_or_other"),
    ("8-K", ["8.01"], "", "contract_or_other"),
    ("8-K", ["2.02", "9.01"], "", "earnings_result"),
    ("8-K", ["3.01"], "", "listing_notice"),
    ("8-K", ["3.02"], "", "offering"),
    ("8-K", ["1.01", "3.02"], "", "offering"),
    ("8-K", ["2.02", "8.01"], "", "earnings_result"),
    ("8-K", ["3.01", "2.02"], "", "listing_notice"),
    ("8-K", ["5.03"], "1-for-10 reverse stock split", "reverse_split"),
    ("8-K", ["5.03"], "Change in fiscal year", None),
    ("8-K", ["5.02"], "", None),
    ("8-K", [], "", None),
    ("8-K/A", ["2.02"], "", "earnings_result"),
    ("DEF 14A", [], "Proposal to approve a reverse split", "reverse_split"),
    ("DEF 14A", [], "Annual meeting", None),
    ("S-1", [], "", "offering"), ("S-1/A", [], "", "offering"), ("S-3", [], "", "offering"),
    ("S-3ASR", [], "", "offering"), ("F-1", [], "", "offering"), ("F-3", [], "", "offering"),
    ("424B5", [], "", "offering"), ("424B3", [], "", "offering"), ("EFFECT", [], "", "offering"),
    ("NT 10-Q", [], "", "late_filing"), ("NT 10-K", [], "", "late_filing"),
    ("10-Q", [], "", None), ("6-K", [], "", None), ("S-8", [], "", None), ("", [], "", None),
])
def test_filing_class_table(form, items, desc, cls):
    assert filing_class(form, items, desc) == cls


@pytest.mark.parametrize("title", [
    "Acme to Announce Financial Results for Third Quarter 2026 on November 3, 2026",
    "Acme Corp. to Report Financial Results for the Third Quarter 2026 on November 12",
    "Acme Schedules Conference Call to Discuss Financial Results for Third Quarter",
    "Award-Winning Acme to Release Q3 Results on November 12",  # "award" (contract) also matches
    "Acme Order Book Update; Third Quarter Results to Be Released November 12",
    "Acme Announces Third Quarter 2026 Earnings Release Date",
])
def test_a_results_date_notice_is_earnings_date_not_a_catalyst(title):
    """Regression: a notice that only names a results date matched "financial results for"
    (earnings_result) or "award"/"order" (contract) first, so gate 6 counted it as a catalyst and
    gate 5 never read its date."""
    assert wire_class(title, KW) == "earnings_date"


def test_real_results_are_still_earnings_result():
    assert wire_class("Acme Announces Financial Results for Third Quarter 2026", KW) == "earnings_result"
    assert wire_class("Acme Reports Third Quarter 2026 Results", KW) == "earnings_result"
