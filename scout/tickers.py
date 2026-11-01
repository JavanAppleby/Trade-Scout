"""Ticker normaliser for wire releases (SCOUT_SPEC 6.3, DESIGN 2.2). No LLM.

Exchange tags look like "(NASDAQ: ACME)", "(NYSE American: ACME)", "(OTCQB: ACME)" or
"(NASDAQ: GOOGL, GOOG)". The prefix list is config (`ticker_exchange_prefixes`); matching is
case-insensitive and the longest prefix wins ("NYSE American" before "NYSE"). Inside one tag a
ticker without its own prefix takes the previous prefix ("GOOG" above is NASDAQ). Segments are split
on ",", ";", "and" and "&" ("(Nasdaq: ACME and ACMEW)"). A segment with an unknown prefix ("TSX: ACM")
is skipped and clears the inherited prefix, so "(TSX: ACM; NASDAQ: ACME)" still gives NASDAQ ACME
and ACM is never taken as a NASDAQ ticker. HTML tags are removed first ("(NASDAQ: <a ...>ACME</a>)").
Each (exchange, ticker) pair appears once, in order.
"""

from __future__ import annotations

import functools
import html
import re
from collections.abc import Iterable, Sequence

__all__ = ["normalise", "tickers_in"]

_TAG = re.compile(r"\(([^()]{1,200})\)")
_SYMBOL = r"[A-Za-z][A-Za-z0-9]{0,5}(?:[.\-/][A-Za-z0-9]{1,2})?"
_HTML_TAG = re.compile(r"<[^<>]{0,500}>")
_SPLIT = re.compile(r"[,;]|\s+(?:and|&)\s+", re.IGNORECASE)
_ANY_PREFIX = re.compile(r"^\s*[A-Za-z][A-Za-z .\-]{0,30}\s*:")


def normalise(sym: str) -> str:
    """Upper case; "." and "/" become "-" (Radar's `normalise_ticker`; EDGAR writes BRK-B)."""
    return sym.strip().upper().replace(".", "-").replace("/", "-")


@functools.lru_cache(maxsize=32)
def _patterns(prefixes: tuple[str, ...]) -> tuple[re.Pattern[str], re.Pattern[str]]:
    ordered = sorted({p.strip() for p in prefixes if p.strip()}, key=len, reverse=True)
    alternation = "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in ordered) or r"(?!x)x"
    with_prefix = re.compile(rf"^\s*(?P<ex>{alternation})\s*:\s*(?P<sym>{_SYMBOL})\s*$", re.IGNORECASE)
    bare = re.compile(rf"^\s*(?P<sym>{_SYMBOL})\s*$")
    return with_prefix, bare


def _canonical(found: str, prefixes: Sequence[str]) -> str:
    """The config spelling of a matched prefix (the first one equal ignoring case and spacing)."""
    exact = " ".join(found.split())
    for p in prefixes:
        if " ".join(p.split()) == exact:
            return p.strip()
    key = exact.lower()
    for p in prefixes:
        if " ".join(p.split()).lower() == key:
            return p.strip()
    return found.strip()  # pragma: no cover - the regex only matches configured prefixes


def tickers_in(text: str, prefixes: Iterable[str]) -> list[tuple[str, str]]:
    """Every (exchange, TICKER) named by an exchange tag in `text`, in order, without repeats."""
    prefix_tuple = tuple(prefixes)
    with_prefix, bare = _patterns(prefix_tuple)
    out: list[tuple[str, str]] = []
    plain = html.unescape(_HTML_TAG.sub("", text or ""))
    for tag in _TAG.finditer(plain):
        exchange: str | None = None
        for segment in _SPLIT.split(tag.group(1)):
            m = with_prefix.match(segment)
            if m:
                exchange = _canonical(m.group("ex"), prefix_tuple)
                sym = m.group("sym")
            elif _ANY_PREFIX.match(segment):
                exchange = None  # another exchange (TSX, LSE ...): skip it and what inherits from it
                continue
            elif exchange is None and bare.match(segment) is not None:
                continue  # a bare symbol after an unknown exchange belongs to that exchange
            elif exchange is not None and (b := bare.match(segment)) is not None:
                sym = b.group("sym")
            else:
                break
            pair = (exchange, normalise(sym))
            if pair not in out:
                out.append(pair)
    return out
