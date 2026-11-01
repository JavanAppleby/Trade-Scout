"""Deterministic class of a wire title or a filing (SCOUT_SPEC 6.4, DESIGN 2.2). No LLM.

Wire titles: the keyword lists are config (`class_keywords`); the first class in the fixed order
`WIRE_MATCH_ORDER` with a matching keyword wins, `other` last. The order is SS 6.4's with one change:
`earnings_date` is tried before `contract` and `earnings_result`. A date notice such as "Acme to
Announce Financial Results for Third Quarter 2026 on November 3" also matches "financial results
for" (earnings_result) and often "agreement" or "order" (contract); classed that way it would make
gate 6 answer true for a press release that only names a date, and gate 5 (which reads the date
from `earnings_date` facts only) would never see it. A keyword matches as whole words, ignoring case
("order" matches "Purchase Order" but not "Reorder"). Filings: `filing_class` (the SS 6.4 table, in
`scout.edgar_parse`).
"""

from __future__ import annotations

import functools
import re
from collections.abc import Mapping, Sequence

from scout.edgar_parse import filing_class
from scout.models import WIRE_CLASSES

__all__ = ["WIRE_MATCH_ORDER", "filing_class", "keyword_hit", "wire_class"]

WIRE_MATCH_ORDER = ("offering", "reverse_split", "listing_notice", "fda", "trial", "earnings_date", "contract",
                    "earnings_result", "other")
assert sorted(WIRE_MATCH_ORDER) == sorted(WIRE_CLASSES)


@functools.lru_cache(maxsize=1024)
def _pattern(keyword: str) -> re.Pattern[str]:
    words = r"\s+".join(re.escape(w) for w in keyword.split())
    return re.compile(rf"(?<![A-Za-z0-9]){words}(?![A-Za-z0-9])", re.IGNORECASE)


def keyword_hit(title: str, keywords: Sequence[str]) -> str | None:
    """The first keyword of `keywords` found in `title` (whole words, any case), or None."""
    for kw in keywords:
        if kw.strip() and _pattern(kw.strip()).search(title or ""):
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
