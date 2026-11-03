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
    # bankruptcy (owner, 2026-11-04) goes first, whatever else the title names
    ("Mercer International Files Voluntary Chapter 11 Petitions", "bankruptcy"),
    ("Acme Enters Restructuring Support Agreement and Files for Bankruptcy Protection", "bankruptcy"),
    ("Acme Obtains Court Approval of Chapter 15 Recognition", "bankruptcy"),
    ("Acme to Report Third Quarter Results; Files Chapter 11", "bankruptcy"),
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
    ("8-K", ["7.01"], "", "other"),  # a press release alone is never a catalyst (owner, 2026-11-04)
    ("8-K", ["8.01", "9.01"], "", "other"),
    ("8-K", ["7.01", "8.01"], "", "other"),
    ("8-K", ["1.01", "8.01"], "", "contract_or_other"),
    ("8-K", ["1.03"], "", "bankruptcy"),
    ("8-K", ["1.01", "1.03", "2.03", "7.01"], "", "bankruptcy"),  # Mercer, 3 Nov 2026: was contract_or_other
    ("8-K", ["1.03", "3.02"], "", "bankruptcy"),
    ("8-K/A", ["1.03"], "", "bankruptcy"),  # a blocking class is kept on an amendment
    ("8-K/A", ["8.01"], "", None),
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
    ("8-K/A", ["2.02"], "", None),  # an amendment is not fresh news
    ("DEF 14A", [], "Proposal to approve a reverse split", "reverse_split"),
    ("DEF 14A", [], "Annual meeting", None),
    ("S-1", [], "", "offering"), ("S-1/A", [], "", "offering"), ("S-3", [], "", "offering"),
    ("S-3ASR", [], "", "offering"), ("F-1", [], "", "offering"), ("F-3", [], "", "offering"),
    ("424B5", [], "", "offering"), ("424B3", [], "", "offering"), ("EFFECT", [], "", "offering"),
    ("424B1", [], "", "offering"), ("424B4", [], "", "offering"), ("424B7", [], "", "offering"),
    ("424B2", [], "", None), ("424B8", [], "", None), ("424b2", [], "", None),  # banks' notes (owner, 2026-11-04)
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


@pytest.mark.parametrize("title", [
    "Acme Will Host Conference Call on November 12 to Discuss Financial Results for the Third Quarter 2026",
    "Acme Announces Timing of Financial Results for Third Quarter 2026",
    "Acme Schedules Conference Call and Webcast for Financial Results for the Third Quarter Ended "
    "September 30, 2026",
    "Acme Sets Date for Release of Financial Results for Third Quarter 2026",
])
def test_more_date_notice_wordings_are_earnings_date(title):
    """Regression: these date notices matched no earnings_date keyword, so "financial results for"
    classed them earnings_result and gate 6 counted a notice that only names a date as a catalyst."""
    assert wire_class(title, KW) == "earnings_date"


@pytest.mark.parametrize("title, cls", [
    ("Acme Signs Distribution Agreement to Release Its Products in Europe", "contract"),
    ("Acme Awarded $40 Million Contract; Will Announce Further Details at Investor Day", "contract"),
    ("Acme Enters Partnership With Beta to Announce Joint Product", "contract"),
    ("Acme Reports Third Quarter 2026 Results and Will Host Conference Call to Discuss", "earnings_result"),
    ("Acme Reports Record Q3 Revenue, Raises Guidance; Conference Call to Discuss Results Today at 4:30 PM ET",
     "other"),
])
def test_contract_and_results_titles_are_not_date_notices(title, cls):
    """Regression: the bare earnings_date phrases ("to release", "will announce", "conference call to
    discuss"), tried first, caught contract and results headlines: gate 6 lost the catalyst and the
    dateless earnings_date fact made gate 5 unknown for 45 days."""
    assert wire_class(title, KW) == cls


def test_a_gap_keyword_needs_both_parts_in_order():
    assert keyword_hit("Acme to Announce Q3 Results", ["to announce ... results"]) is not None
    assert keyword_hit("Results: Acme to Announce", ["to announce ... results"]) is None
    assert keyword_hit("Acme to Announcements results", ["to announce ... results"]) is None
    assert keyword_hit("x", [" ... "]) is None


# --------------------------------------------------------------------------- third review


@pytest.mark.parametrize("title", [
    "Acme Bio to Report Third Quarter 2026 Financial Results and Phase 2 Topline Data on November 12, 2026",
    "Acme Bio to Host Conference Call on November 12 to Discuss Third Quarter Results and PDUFA Update",
])
def test_a_biotech_date_notice_is_earnings_date_not_fda_or_trial(title):
    """Regression: fda and trial were tried before earnings_date, so a date notice that also names
    trial data or a PDUFA update became a gate-6 catalyst and gate 5 never read its date."""
    assert wire_class(title, KW) == "earnings_date"


@pytest.mark.parametrize("title", [
    "Acme Enters into At The Market Sales Agreement with H.C. Wainwright",
    "Acme Enters into At-the-Market Sales Agreement with H.C. Wainwright",
    "Acme Enters into Equity Distribution Agreement for Up to $25 Million",
    "Acme Announces $50 Million Equity Purchase Agreement with Lincoln Park",
    "Acme Enters into Standby Equity Purchase Agreement with Yorkville",
    "Acme Announces Warrant Inducement Agreement for $4.2 Million Gross Proceeds",
    "Acme Enters into Convertible Note Agreement",
    "Acme Announces Debt Exchange Agreement and Settlement of Notes",
    "Acme Announces Immediate Exercise of Existing Warrant",
    "Acme Announces ATM Program",
    "Acme Enters into Securities Purchase Agreement with Institutional Investors",
])
def test_dilution_agreements_are_offering_not_contract(title):
    """Regression: "agreement" (contract) caught equity lines, ATM sales agreements, warrant
    inducements and convertible notes, so a dilution headline made gate 6 true."""
    assert wire_class(title, KW) == "offering"


@pytest.mark.parametrize("title", [
    "Acme Announces Conference Call to Discuss Financial Results for the Third Quarter 2026 on October 29",
    "Acme Sets Third Quarter 2026 Earnings Call for October 29",
    "Acme Announces Third Quarter 2026 Earnings Release and Conference Call Date",
    "Acme Announces Q3 2026 Financial Results Release Date",
    "Acme Announces Third Quarter 2026 Financial Results Conference Call and Webcast on October 29",
    "Acme to Webcast Third Quarter 2026 Results on October 29",
    "Acme Provides Notice of Third Quarter 2026 Earnings Call on October 29",
])
def test_common_date_notice_wordings_are_earnings_date(title):
    """Regression: these fell to earnings_result ("financial results for", a gate-6 class) or to
    other, so gate 5 lost its wire source."""
    assert wire_class(title, KW) == "earnings_date"


@pytest.mark.parametrize("title, cls", [
    ("Acme Announces Positive Phase 2b Data", "trial"),
    ("Acme Announces Positive Phase IIb Data", "trial"),
    ("Acme Receives Breakthrough Device Designation", "fda"),
    ("Acme Reports Q3 2026 Results", "earnings_result"),
    ("Acme Signs Distribution Agreement with Beta", "contract"),  # a plain agreement is still a contract
])
def test_fail_closed_misses_are_classed(title, cls):
    assert wire_class(title, KW) == cls


_CAPTIONS = {
    "3.03": "Item 3.03: Material Modification to Rights of Security Holders",
    "5.03": "Item 5.03: Amendments to Articles of Incorporation or Bylaws; Change in Fiscal Year",
    "7.01": "Item 7.01: Regulation FD Disclosure",
    "8.01": "Item 8.01: Other Events",
    "9.01": "Item 9.01: Financial Statements and Exhibits",
    "1.01": "Item 1.01: Entry into a Material Definitive Agreement",
}


def _live_description(items):
    """What edgar_atom passes: the entry title plus the summary, which carries only item captions."""
    summary = "Filed: 2026-11-02 AccNo: 0001234567-26-000123 Size: 250 KB " + " ".join(_CAPTIONS[i] for i in items)
    return "8-K - ACME CORP (0001234567) (Filer) " + summary


@pytest.mark.parametrize("items, cls", [
    (["5.03", "8.01", "9.01"], "other"),
    (["5.03", "7.01", "9.01"], "other"),
    (["3.03", "5.03", "9.01"], "reverse_split"),
    (["3.03", "5.03", "8.01", "9.01"], "reverse_split"),
    (["3.03", "8.01"], "other"),
    (["1.01", "5.03"], "contract_or_other"),  # a material agreement still counts
])
def test_a_reverse_split_8k_from_live_captions_is_never_a_catalyst(items, cls):
    """Regression (third review): the live description never contains "reverse", so a reverse-split
    8-K (5.03 with 8.01 or 3.03) was classed contract_or_other through its press-release item and
    made gate 6 true."""
    assert filing_class("8-K", items, _live_description(items)) == cls


def test_bankruptcy_keywords_may_be_left_out_of_an_older_table(scout_dir):  # noqa: ANN001
    """A [class_keywords] table written before 2026-11-04 has no bankruptcy list: the default stands."""
    from scout.config import DEFAULT_CLASS_KEYWORDS, config_from_dict  # noqa: PLC0415
    from scout.config_load import ConfigError  # noqa: PLC0415

    table = {k: list(v) for k, v in DEFAULT_CLASS_KEYWORDS.items() if k != "bankruptcy"}
    cfg = config_from_dict({"class_keywords": table}, scout_dir)
    assert cfg.class_keywords["bankruptcy"] == DEFAULT_CLASS_KEYWORDS["bankruptcy"]
    del table["offering"]
    with pytest.raises(ConfigError, match="missing offering"):
        config_from_dict({"class_keywords": table}, scout_dir)


def test_contract_or_other_is_no_longer_a_default_catalyst():
    assert "contract_or_other" not in ScoutConfig().hold_catalyst_classes
