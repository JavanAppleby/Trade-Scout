"""Deterministic post-checks on every label (SCOUT_SPEC 8.4, DESIGN 2.6).

A label is `verified` only when every check returns no failure. An unverified label is still kept
(with its failures) and every reader treats it as unknown. The checks:
- the ticker is in the input ticker set and every evidence id exists in the input;
- J3: every cited URL appears in a `web_search_tool_result` or `web_fetch_tool_result` block of the
  same response, its host is in `llm_allowed_domains`, the page fetched in that response contains
  the quote verbatim, and the ticker or company name appears in it;
- a scheduled date falls within -7..+200 days and its quote appears verbatim in a source about the
  label's own symbol: its own item, an item whose ticker set holds the symbol, or (J3) a fetched
  page that names the symbol or company. A quote from another ticker's item or page never counts;
- an input item that tries to instruct the model must come back as `promotion_suspected`; any
  other label for it fails (`injection_in_input`), so a hostile item never yields a passing check.
  The whole batch was exposed to it, so every other label in the same request fails too
  (`injection_in_batch`): an injected item can steer the labels of the items next to it.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlsplit

DATE_MIN_DAYS = -7
DATE_MAX_DAYS = 200
QUOTE_MIN_CHARS = 10

_INJECTION = re.compile(
    r"(?i)\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|your|these)\b"
    r"[^.\n]{0,20}\b(instructions?|rules?|prompts?|directions?)\b"
    r"|\bsystem\s+prompt\b|\byou\s+are\s+now\b|\bnew\s+instructions?\b|\bas\s+an?\s+(ai|assistant|language model)\b"
    r"|\b(mark|label|classify|tag)\s+(this|it|the item)\s+(as\s+)?(an?\s+)?(fda|approval|positive|catalyst|verified)"
    r"|</?\s*item\b|\bnonce\s*=")

__all__ = [
    "DATE_MAX_DAYS", "DATE_MIN_DAYS", "QUOTE_MIN_CHARS", "check_date", "check_hostile", "check_j3", "check_label",
    "fetched_pages", "host_allowed", "injection_suspected", "names_symbol", "norm_url", "result_urls",
]


def injection_suspected(text: object) -> bool:
    return bool(_INJECTION.search(html.unescape(str(text or ""))))


def _norm_text(text: object) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(text or ""))).strip()


def norm_url(url: object) -> str:
    s = str(url or "").strip()
    parts = urlsplit(s)
    path = parts.path.rstrip("/") or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}{path}{query}"


def host_allowed(url: object, allowed_domains: Iterable[str]) -> bool:
    parts = urlsplit(str(url or ""))
    if parts.scheme not in ("https", "http"):
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    for d in allowed_domains:
        d = d.lower().strip().lstrip(".")
        if d and (host == d or host.endswith("." + d)):
            return True
    return False


# --------------------------------------------------------------------------- label basics


def check_label(label: Mapping[str, Any], input_tickers: Iterable[str], evidence_ids: Iterable[str]) -> list[str]:
    failures: list[str] = []
    tickers = {t.upper() for t in input_tickers if t}
    if str(label.get("ticker") or "").upper() not in tickers:
        failures.append("ticker_not_in_input")
    known = set(evidence_ids)
    for eid in label.get("evidence_ids") or ():
        if eid not in known:
            failures.append(f"evidence_id_unknown:{str(eid)[:40]}")
    return failures


def check_hostile(label: Mapping[str, Any], item_texts: Iterable[str]) -> list[str]:
    """An instruction inside an input item: only `promotion_suspected` may pass."""
    if any(injection_suspected(t) for t in item_texts) and label.get("catalyst_type") != "promotion_suspected":
        return ["injection_in_input"]
    return []


def check_date(event: Any, source_texts: Iterable[str], today: date) -> list[str]:
    """A scheduled event: date within -7..+200 days of `today`, quote verbatim in a source text."""
    if event is None:
        return []
    if not isinstance(event, Mapping):
        return ["event_not_object"]
    failures: list[str] = []
    try:
        day = date.fromisoformat(str(event.get("date") or ""))
    except ValueError:
        failures.append("event_date_unreadable")
    else:
        if not (today + timedelta(days=DATE_MIN_DAYS) <= day <= today + timedelta(days=DATE_MAX_DAYS)):
            failures.append("event_date_out_of_range")
    quote = _norm_text(event.get("quote"))
    if len(quote) < QUOTE_MIN_CHARS:
        failures.append("event_quote_too_short")
    elif not any(quote in _norm_text(t) for t in source_texts):
        failures.append("event_quote_not_in_source")
    return failures


# --------------------------------------------------------------------------- J3 citations


def result_urls(blocks: Sequence[Mapping[str, Any]]) -> set[str]:
    """Every URL in this response's search and fetch result blocks (normalised)."""
    urls: set[str] = set()
    for b in blocks:
        content = b.get("content")
        if b.get("type") == "web_search_tool_result" and isinstance(content, list):
            urls.update(norm_url(r.get("url")) for r in content if isinstance(r, Mapping) and r.get("url"))
        elif b.get("type") == "web_fetch_tool_result" and isinstance(content, Mapping) and content.get("url"):
            urls.add(norm_url(content.get("url")))
    return urls


def fetched_pages(blocks: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """URL (normalised) -> fetched text, from this response's web_fetch results (text documents)."""
    pages: dict[str, str] = {}
    for b in blocks:
        content = b.get("content")
        if b.get("type") != "web_fetch_tool_result" or not isinstance(content, Mapping):
            continue
        if content.get("type") != "web_fetch_result" or not content.get("url"):
            continue
        doc = content.get("content")
        source = doc.get("source") if isinstance(doc, Mapping) else None
        if isinstance(source, Mapping) and source.get("type") == "text" and isinstance(source.get("data"), str):
            key = norm_url(content.get("url"))
            pages[key] = pages.get(key, "") + "\n" + source["data"]
    return pages


def names_symbol(text: str, ticker: str, company: str | None = None) -> bool:
    """True when `text` names `ticker` (whole word) or `company`."""
    return _names_symbol(_norm_text(text), str(ticker or "").upper(), company)


def _names_symbol(text: str, ticker: str, company: str | None) -> bool:
    if ticker and re.search(rf"(?<![A-Za-z0-9]){re.escape(ticker)}(?![A-Za-z0-9])", text):
        return True
    return bool(company) and _norm_text(company).lower() in text.lower()


def check_j3(label: Mapping[str, Any], citations: Sequence[Mapping[str, Any]],
             response_blocks: Sequence[Mapping[str, Any]], allowed_domains: Iterable[str], *,
             company: str | None = None) -> list[str]:
    domains = list(allowed_domains)
    if not citations:
        return ["no_citations"]
    seen = result_urls(response_blocks)
    pages = fetched_pages(response_blocks)
    ticker = str(label.get("ticker") or "").upper()
    failures: list[str] = []
    for n, cite in enumerate(citations):
        url = norm_url(cite.get("url"))
        tag = f"citation{n}"
        if not host_allowed(cite.get("url"), domains):
            failures.append(f"{tag}:host_not_allowed")
        if url not in seen:
            failures.append(f"{tag}:url_not_in_results")
            continue
        page = pages.get(url)
        if page is None:
            failures.append(f"{tag}:url_not_fetched")
            continue
        text = _norm_text(page)
        quote = _norm_text(cite.get("quote"))
        if len(quote) < QUOTE_MIN_CHARS:
            failures.append(f"{tag}:quote_too_short")
        elif quote not in text:
            failures.append(f"{tag}:quote_not_in_page")
        if not _names_symbol(text, ticker, company):
            failures.append(f"{tag}:symbol_not_named")
    return failures
