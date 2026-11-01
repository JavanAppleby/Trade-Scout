"""scout_reader.datetext: the announced-date parser for gate 5 source 1 (SCOUT_SPEC 7.2 item 1)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scout_reader.datetext import announced_date, dates_in

PUB = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)  # Thursday 29 Oct 2026, 08:00 ET


@pytest.mark.parametrize("title, expected", [
    ("Acme Corp. to Report Third Quarter 2026 Financial Results on November 12, 2026", date(2026, 11, 12)),
    ("Acme to Announce Q3 Results Nov. 12", date(2026, 11, 12)),
    ("Acme to announce results on Nov 12, 2026 after market close", date(2026, 11, 12)),
    ("Acme Will Release Earnings on Thursday, November 12, 2026", date(2026, 11, 12)),
    ("Acme to Report Results on November 12th", date(2026, 11, 12)),
    ("Acme Sets Date: Sept. 3 2027 Results Call", date(2027, 9, 3)),
    ("ACME TO REPORT RESULTS ON NOVEMBER 12, 2026", date(2026, 11, 12)),
    # a period end is skipped; the event date wins
    ("Acme to Report Results for the Quarter Ended September 30, 2026 on November 12, 2026", date(2026, 11, 12)),
    ("Acme to Release Fiscal Results for Period Ending Sept. 30 on Nov. 5", date(2026, 11, 5)),
    # "on" decides between two dates
    ("Acme, Rescheduled From November 10, to Report on November 12, 2026", date(2026, 11, 12)),
])
def test_parses(title: str, expected: date) -> None:
    assert announced_date(title, PUB) == expected


def test_year_less_date_uses_the_published_year_and_wraps() -> None:
    december = datetime(2026, 12, 15, 15, 0, tzinfo=timezone.utc)
    assert announced_date("Acme to Report Results on Jan. 14", december) == date(2027, 1, 14)
    assert announced_date("Acme to Report Results on Dec. 20", december) == date(2026, 12, 20)
    # published as a plain date also works
    assert announced_date("Acme to Report Results on Nov. 12", date(2026, 10, 29)) == date(2026, 11, 12)


@pytest.mark.parametrize("title", [
    "Acme to Report Third Quarter 2026 Financial Results",  # no date
    "Acme to Report Results on 11/12/2026",  # numeric: order ambiguous, never read
    "Acme to Report Results Nov. 12-13",  # a range
    "Acme to Report Results November 12 to 13, 2026",
    "Acme to Report Results on Wednesday, November 12, 2026",  # wrong weekday (12 Nov 2026 is a Thursday)
    "Acme to Report Results on February 30, 2027",  # impossible
    "Acme to Report on November 12, 2026; Webcast on November 13, 2026",  # two different "on" dates
    "Acme Investor Day November 10 and Results November 12",  # two plain dates
    "",
])
def test_ambiguous_or_missing_is_unparsed(title: str) -> None:
    assert announced_date(title, PUB) is None


def test_year_less_without_a_publication_time_is_unparsed() -> None:
    assert announced_date("Acme to Report Results on Nov. 12", None) is None
    assert announced_date("Acme to Report Results on Nov. 12", datetime(2026, 10, 29, 12, 0)) is None  # naive
    assert announced_date("Acme to Report Results on Nov. 12, 2026", None) == date(2026, 11, 12)


def test_may_the_verb_is_not_a_month() -> None:
    assert announced_date("Acme may 5 times raise; to report on June 4, 2027", PUB) == date(2027, 6, 4)
    assert announced_date("Acme to Report Results on May 6, 2027", PUB) == date(2027, 5, 6)


def test_words_that_contain_month_names_are_not_dates() -> None:
    assert dates_in("Acme Market Update: Marching Ahead, Decent Junebug 12", PUB) == []


def test_non_string_title_is_unparsed() -> None:
    assert announced_date(None, PUB) is None
    assert announced_date(12, PUB) is None


@settings(max_examples=300, deadline=None)
@given(st.text(max_size=200), st.one_of(st.none(), st.datetimes(timezones=st.just(timezone.utc))))
def test_never_raises(title: str, published: datetime | None) -> None:
    got = announced_date(title, published)
    assert got is None or isinstance(got, date)


@pytest.mark.parametrize("title", [
    "Acme, Which Closed Its Acquisition on October 30, to Report Third Quarter Results November 3",
    "Acme Completed Merger on October 30; Third Quarter Results November 3, 2026",
])
def test_an_on_date_that_disagrees_with_a_plain_date_is_ambiguous(title: str) -> None:
    """Regression: an "on <date>" mention used to win over the other dates and gave 30 October (a
    wrong date, so gate 5 recorded neither a hit nor unparsed_date). Disagreement is now None."""
    assert announced_date(title, PUB) is None


def test_a_rescheduled_from_date_is_the_old_date_and_skipped() -> None:
    assert announced_date("Acme Moves Results Call, Postponed From November 10, to November 12, 2026", PUB) \
        == date(2026, 11, 12)
