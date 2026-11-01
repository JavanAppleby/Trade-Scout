"""Deterministic class of a wire title or a filing (SCOUT_SPEC 6.4, DESIGN 2.2). No LLM.

Wire titles: the keyword lists are config (`class_keywords`); the first class in the fixed order
`WIRE_MATCH_ORDER` with a matching keyword wins, `other` last. The order is SS 6.4's with one change:
`earnings_date` is tried first. A date notice such as "Acme to Announce Financial Results for Third
Quarter 2026 on November 3" also matches "financial results for" (earnings_result), often
"agreement" or "order" (contract), and for a biotech "Phase 2", "topline" or "PDUFA" (trial, fda):
"Acme Bio to Report Third Quarter 2026 Financial Results and Phase 2 Topline Data on November 12".
Classed any of those ways it would make gate 6 answer true for a press release that only names a
date, and gate 5 (which reads the date from `earnings_date` facts only) would never see it. A title
that names a results date is therefore `earnings_date` whatever else it names (third review; the
second review moved it ahead of contract and earnings_result only), and `earnings_date` never
passes gate 6 (`scout_reader.gates.NEVER_PASS`). Because earnings_date goes first, its keywords
must be narrow (SS 6.4: "to report / announce / release ... results"): each default keyword pairs a
scheduling phrase with the subject that follows it, written with a `...` gap ("will host ...
results", "timing of ... earnings"). A bare "to release" or "will announce" would also catch
"Agreement to Release Its Products" (contract) or "Reports Q3 Results and Will Host Conference Call
to Discuss" (earnings_result), and an earnings_date fact with no date in it makes gate 5 `unknown`
for `earnings_lookback_days` (fail closed: "Acme to Host Call to Discuss Phase 3 Topline Results"
with no date costs holds, never allows one).

A keyword matches as whole words, ignoring case ("order" matches "Purchase Order" but not
"Reorder"). ` ... ` inside a keyword matches any text, so "to announce ... results" needs both parts
in that order. Filings: `filing_class` (the SS 6.4 table, in `scout.edgar_parse`).
"""

from __future__ import annotations

import functools
import re
from collections.abc import Mapping, Sequence

from scout.edgar_parse import filing_class
from scout.models import WIRE_CLASSES

__all__ = ["WIRE_MATCH_ORDER", "filing_class", "keyword_hit", "wire_class"]

WIRE_MATCH_ORDER = ("earnings_date", "offering", "reverse_split", "listing_notice", "fda", "trial", "contract",
                    "earnings_result", "other")
assert sorted(WIRE_MATCH_ORDER) == sorted(WIRE_CLASSES)


GAP = "..."


def _phrase(part: str) -> str:
    words = r"\s+".join(re.escape(w) for w in part.split())
    return rf"(?<![A-Za-z0-9]){words}(?![A-Za-z0-9])"


@functools.lru_cache(maxsize=1024)
def _pattern(keyword: str) -> re.Pattern[str]:
    parts = [p for p in (x.strip() for x in keyword.split(GAP)) if p]
    return re.compile(".*?".join(_phrase(p) for p in parts), re.IGNORECASE | re.DOTALL)


def keyword_hit(title: str, keywords: Sequence[str]) -> str | None:
    """The first keyword of `keywords` found in `title` (whole words, any case; ` ... ` is a gap), or None."""
    for kw in keywords:
        if kw.strip(" .") and _pattern(kw.strip()).search(title or ""):
            return kw
    return None


def wire_class(title: str, keywords: Mapping[str, Sequence[str]]) -> str:
    """The class of a wire title: first match in `WIRE_MATCH_ORDER`, else "other"."""
    for cls in WIRE_MATCH_ORDER:
        if cls == "other":
            break
        if keyword_hit(title, tuple(keywords.get(cls, ()))) is not None:
            return cls
    return "other"
