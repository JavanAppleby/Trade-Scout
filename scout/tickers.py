"""Ticker normaliser for wire releases (SCOUT_SPEC 6.3, DESIGN 2.2). No LLM.

Exchange tags look like "(NASDAQ: ACME)", "(NYSE American: ACME)", "(OTCQB: ACME)" or
"(NASDAQ: GOOGL, GOOG)". The prefix list is config (`ticker_exchange_prefixes`); matching is
case-insensitive and the longest prefix wins ("NYSE American" before "NYSE"). Inside one tag a
ticker without its own prefix takes the previous prefix ("GOOG" above is NASDAQ). Segments are split
on ",", ";", "and", "&" and a "/" that comes before another prefix ("(Nasdaq: ACME and ACMEW)",
"(CSE: ACM / OTCQB: ACMEF)"). A segment with an unknown prefix ("TSX: ACM") or any other text
('the "Company"') is skipped and clears the inherited prefix, so "(TSX: ACM; NASDAQ: ACME)" and
'(the "Company", NASDAQ: ACME)' still give NASDAQ ACME and ACM is never taken as a NASDAQ ticker. The
prefix is followed by ":" or by a dash with a space each side ("(Nasdaq - ACME)"; "Nasdaq-listed" is
not a tag). HTML tags are removed first ("(NASDAQ: <a ...>ACME</a>)"). Each (exchange, ticker) pair
appears once, in order.

`title_suffix_ticker` reads the one ticker a reposting site puts at the end of a title, after a "|"
and before a configured suffix (`wire_title_ticker_suffixes`): Stock Titan writes
"Acme Prices Offering | ACME Stock News". The symbol must be upper case; the exchange is not named.
"""

from __future__ import annotations

import functools
import html
import re
from collections.abc import Iterable, Sequence

__all__ = ["normalise", "tickers_in", "title_suffix_ticker"]

_TAG = re.compile(r"\(([^()]{1,200})\)")
_SYMBOL = r"[A-Za-z][A-Za-z0-9]{0,5}(?:[.\-/][A-Za-z0-9]{1,2})?"
_HTML_TAG = re.compile(r"<[^<>]{0,500}>")
_SPLIT = re.compile(r"[,;]|\s+(?:and|&)\s+|\s*/\s*(?=[A-Za-z][A-Za-z .\-]{0,30}\s*:)", re.IGNORECASE)
_ANY_PREFIX = re.compile(r"^\s*[A-Za-z][A-Za-z .\-]{0,30}\s*:")
_UPPER_SYMBOL = r"[A-Z][A-Z0-9]{0,5}(?:[.\-/][A-Z0-9]{1,2})?"


def normalise(sym: str) -> str:
    """Upper case; "." and "/" become "-" (Radar's `normalise_ticker`; EDGAR writes BRK-B)."""
    return sym.strip().upper().replace(".", "-").replace("/", "-")


@functools.lru_cache(maxsize=32)
def _patterns(prefixes: tuple[str, ...]) -> tuple[re.Pattern[str], re.Pattern[str]]:
    ordered = sorted({p.strip() for p in prefixes if p.strip()}, key=len, reverse=True)
    alternation = "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in ordered) or r"(?!x)x"
    with_prefix = re.compile(rf"^\s*(?P<ex>{alternation})(?:\s*:|\s+[-\u2013\u2014])\s*(?P<sym>{_SYMBOL})\s*$",
                             re.IGNORECASE)
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
                exchange = None  # other text ('the "Company"'): skip it, keep reading the tag
                continue
            pair = (exchange, normalise(sym))
            if pair not in out:
                out.append(pair)
    return out


@functools.lru_cache(maxsize=8)
def _suffix_pattern(suffixes: tuple[str, ...]) -> re.Pattern[str]:
    alternation = "|".join(r"\s+".join(map(re.escape, x.split())) for x in suffixes if x.strip()) or r"(?!x)x"
    return re.compile(rf"\|\s*(?P<sym>{_UPPER_SYMBOL})\s+(?:{alternation})\s*$")


def title_suffix_ticker(title: str, suffixes: Iterable[str]) -> str | None:
    """The TICKER in "... | TICKER <suffix>" at the end of `title`, or None."""
    m = _suffix_pattern(tuple(suffixes)).search(html.unescape(title or ""))
    return normalise(m.group("sym")) if m else None
