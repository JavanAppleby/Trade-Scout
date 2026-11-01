"""Pure EDGAR parsers (SCOUT_SPEC 4.1 #1-3, 6.3, 6.4; DESIGN 2.2).

Part 1 is COPIED from Radar, unchanged except where marked "[Scout]":
    source  Trade-Radar  radar/catalysts_edgar.py
    commit  6c971c8a6bda0c68ed6a8ac261e1cd96ed65eade  (unchanged at Radar main 239bed2)
    sha256  379d46e1db88f1091f9890f9b0a6be9f479d354ab1fa751e6c57e7d6691b03c3
    copied  normalise_ticker, _cik, parse_ticker_map, _column, parse_submissions, _base_form,
            _describe, flags_from_filings, EVIDENCE_PER_KIND; Radar's models.Filing and
            models.EdgarFlags as `RadarFiling` and `EdgarFlags`.
    changed [Scout] `parse_iso` / `to_et` come from scout.clock (Radar takes them from
            trade_guardian.clock, which Scout must not install, SS 2.1); `Filing` is renamed
            `RadarFiling` (scout.models.Filing is the stored shape); `flags_from_filings` takes any
            object with Radar's EDGAR config fields: `RadarConfigAdapter` carries Radar's defaults.
tests/test_edgar_parse.py checks the copy against Radar's own outputs on a shared fixture
(tests/fixtures/wire/radar_shared_submissions.json, produced once by Radar's code).

Part 2 is Scout's: `parse_periodic` (reportDate and fiscalYearEnd), `parse_getcurrent_atom` (the
"getcurrent" Atom page; 8-K items from each summary) and `filing_class` (the SS 6.4 table).
A changed shape raises `SchemaError`: the caller refuses the source, never skips rows.
"""

from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from scout.clock import iso
from scout.clock import parse_iso as _scout_parse_iso
from scout.clock import to_et
from scout.models import Filing

__all__ = [
    "AtomEntry", "AtomPage", "EdgarFlags", "RadarConfigAdapter", "RadarFiling", "SchemaError", "filing_class",
    "flags_from_filings", "normalise_ticker", "parse_getcurrent_atom", "parse_json", "parse_periodic",
    "parse_submissions", "parse_ticker_map", "safe_xml_root",
]


class SchemaError(ValueError):
    """The source's shape changed: refuse the source (status `unavailable`), never skip rows."""


def parse_iso(value: object) -> datetime | None:  # [Scout] Radar's trade_guardian.clock.parse_iso
    return _scout_parse_iso(value)


@dataclass(frozen=True)
class RadarFiling:  # [Scout] Radar's radar.models.Filing, renamed
    form: str
    accepted: datetime
    filing_date: date
    accession: str
    items: tuple[str, ...]


@dataclass(frozen=True)
class EdgarFlags:  # Radar's radar.models.EdgarFlags
    news_filing: bool | None
    dilution_risk: bool | None
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class RadarConfigAdapter:
    """[Scout] The RadarConfig fields `flags_from_filings` reads, with Radar's defaults
    (radar/config.py at 6c971c8)."""

    edgar_news_forms: tuple[str, ...] = ("8-K", "6-K")
    edgar_news_hours: int = 24
    edgar_dilution_forms: tuple[str, ...] = ("S-1", "S-1MEF", "S-3", "S-3ASR", "S-3MEF", "F-1", "F-1MEF", "F-3",
                                             "F-3ASR", "F-3MEF")
    edgar_dilution_prefixes: tuple[str, ...] = ("424B",)
    edgar_dilution_days: int = 30
    edgar_count_amendments: bool = True


# =========================================================================== Part 1: Radar's copy

EVIDENCE_PER_KIND = 3


def normalise_ticker(symbol: str) -> str:
    """Upper case; "." and "/" become "-" (EDGAR writes BRK-B)."""
    return symbol.strip().upper().replace(".", "-").replace("/", "-")


def _cik(value: object) -> int | None:
    try:
        cik = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return cik if cik > 0 else None


def parse_ticker_map(body: object) -> dict[str, tuple[int, str | None]]:
    """`company_tickers_exchange.json` ({"fields": [...], "data": [[cik, name, ticker, exchange]]})
    -> {TICKER: (cik, exchange)}. Also reads the older `company_tickers.json` shape. Tolerant:
    unreadable rows are skipped; the first row of a duplicated ticker wins."""
    out: dict[str, tuple[int, str | None]] = {}

    def add(ticker: object, cik: object, exchange: object) -> None:
        if not isinstance(ticker, str) or not ticker.strip():
            return
        number = _cik(cik)
        if number is None:
            return
        exch = exchange.strip() if isinstance(exchange, str) and exchange.strip() else None
        out.setdefault(normalise_ticker(ticker), (number, exch))

    if not isinstance(body, Mapping):
        return out
    fields, data = body.get("fields"), body.get("data")
    if isinstance(fields, list) and isinstance(data, list):
        names = [str(f).lower() for f in fields]
        if "cik" not in names or "ticker" not in names:
            return out
        i_cik, i_tic = names.index("cik"), names.index("ticker")
        i_exc = names.index("exchange") if "exchange" in names else None
        for row in data:
            if not isinstance(row, (list, tuple)) or len(row) <= max(i_cik, i_tic):
                continue
            exch = row[i_exc] if i_exc is not None and len(row) > i_exc else None
            add(row[i_tic], row[i_cik], exch)
        return out
    for row in body.values():  # company_tickers.json: {"0": {"cik_str", "ticker", "title"}}
        if isinstance(row, Mapping):
            add(row.get("ticker"), row.get("cik_str", row.get("cik")), row.get("exchange"))
    return out


def _column(cols: Mapping[str, object], key: str) -> list[object]:
    value = cols.get(key)
    return list(value) if isinstance(value, list) else []


def parse_submissions(body: object) -> list[RadarFiling]:
    """The columnar `filings.recent` block of a submissions JSON (or a bare columnar block, as in
    the paged `filings.files`). Rows without a form or a readable acceptance time are skipped."""
    if not isinstance(body, Mapping):
        return []
    cols: object = body
    filings = body.get("filings")
    if isinstance(filings, Mapping):
        cols = filings.get("recent")
    if not isinstance(cols, Mapping):
        return []
    forms = _column(cols, "form")
    accepted = _column(cols, "acceptanceDateTime")
    filing_dates = _column(cols, "filingDate")
    accessions = _column(cols, "accessionNumber")
    items = _column(cols, "items")
    out: list[RadarFiling] = []
    for i in range(min(len(forms), len(accepted))):
        form = forms[i]
        at = parse_iso(accepted[i])
        if not isinstance(form, str) or not form.strip() or at is None:
            continue
        try:
            fdate = date.fromisoformat(str(filing_dates[i])) if i < len(filing_dates) else None
        except ValueError:
            fdate = None
        acc = accessions[i] if i < len(accessions) and isinstance(accessions[i], str) else ""
        raw_items = items[i] if i < len(items) and isinstance(items[i], str) else ""
        out.append(RadarFiling(
            form=form.strip().upper(), accepted=at, filing_date=fdate or to_et(at).date(),
            accession=acc, items=tuple(p.strip() for p in raw_items.split(",") if p.strip()),
        ))
    return out


def _base_form(form: str, count_amendments: bool) -> str | None:
    """`X/A` counts as `X` when amendments count, else not at all."""
    f = form.strip().upper()
    if f.endswith("/A"):
        return f[:-2] if count_amendments else None
    return f


def _describe(kind: str, f: RadarFiling) -> str:
    et = to_et(f.accepted)
    return f"{kind}: {f.form} accepted {et:%Y-%m-%d %H:%M} ET ({f.accession or 'no accession'})"


def flags_from_filings(filings: Sequence[RadarFiling], at: datetime,
                       cfg: RadarConfigAdapter) -> EdgarFlags:
    """Pure. Filings accepted after `at` are ignored, so a late fetch never leaks."""
    news_from = at - timedelta(hours=cfg.edgar_news_hours)
    dilution_from = at - timedelta(days=cfg.edgar_dilution_days)
    news_forms = {f.upper() for f in cfg.edgar_news_forms}
    dilution_forms = {f.upper() for f in cfg.edgar_dilution_forms}
    prefixes = tuple(p.upper() for p in cfg.edgar_dilution_prefixes)
    news: list[RadarFiling] = []
    dilution: list[RadarFiling] = []
    for f in filings:
        if f.accepted > at:
            continue
        base = _base_form(f.form, cfg.edgar_count_amendments)
        if base is None:
            continue
        if news_from < f.accepted and base in news_forms:
            news.append(f)
        if dilution_from < f.accepted and (base in dilution_forms or base.startswith(prefixes)):
            dilution.append(f)
    evidence: list[str] = []
    for kind, found in (("news", news), ("dilution", dilution)):
        newest = sorted(found, key=lambda f: f.accepted, reverse=True)
        evidence += [_describe(kind, f) for f in newest[:EVIDENCE_PER_KIND]]
        if len(newest) > EVIDENCE_PER_KIND:
            evidence.append(f"{kind}: +{len(newest) - EVIDENCE_PER_KIND} more")
    return EdgarFlags(bool(news), bool(dilution), tuple(evidence))


# =========================================================================== Part 2: Scout's


def parse_json(body: bytes | str | object) -> object:
    """bytes/str -> JSON value (BOM tolerated); anything else is returned as given. SchemaError
    when the text is not JSON."""
    if isinstance(body, (bytes, bytearray)):
        try:
            body = bytes(body).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SchemaError("not UTF-8") from exc
    if isinstance(body, str):
        try:
            return json.loads(body)
        except ValueError as exc:
            raise SchemaError("not JSON") from exc
    return body


_FYE = re.compile(r"(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])")


def parse_periodic(body: bytes | str | object) -> tuple[list[Filing], str | None]:
    """A submissions JSON -> (every filing in `filings.recent` with its `reportDate`, the company's
    `fiscalYearEnd` "MMDD" or None). Rows are read as Radar's `parse_submissions` reads them (a row
    without a form or a readable acceptance time is skipped); the shape itself must hold, else
    SchemaError."""
    data = parse_json(body)
    if not isinstance(data, Mapping):
        raise SchemaError("submissions: not a JSON object")
    filings = data.get("filings")
    if not isinstance(filings, Mapping) or not isinstance(filings.get("recent"), Mapping):
        raise SchemaError("submissions: no filings.recent block")
    cols = filings["recent"]
    for key in ("form", "acceptanceDateTime", "accessionNumber", "filingDate"):
        if not isinstance(cols.get(key), list):
            raise SchemaError(f"submissions: filings.recent.{key} is missing or not a list")
    forms, accepted = cols["form"], cols["acceptanceDateTime"]
    if len(forms) != len(accepted):
        raise SchemaError("submissions: columns of different lengths")
    report_dates = _column(cols, "reportDate")
    out: list[Filing] = []
    for rf, i in zip(parse_submissions(data), _kept_rows(forms, accepted)):
        rd = report_dates[i] if i < len(report_dates) else None
        report = None
        if isinstance(rd, str) and rd.strip():
            try:
                report = date.fromisoformat(rd.strip()).isoformat()
            except ValueError:
                report = None
        out.append(Filing(form=rf.form, accepted=iso(rf.accepted), filing_date=rf.filing_date.isoformat(),
                          accession=rf.accession, items=rf.items, report_date=report))
    fye = data.get("fiscalYearEnd")
    fiscal = fye.strip() if isinstance(fye, str) and _FYE.fullmatch(fye.strip()) else None
    return out, fiscal


def _kept_rows(forms: list[object], accepted: list[object]) -> list[int]:
    """The row indexes `parse_submissions` keeps (same test, same order)."""
    return [i for i in range(min(len(forms), len(accepted)))
            if isinstance(forms[i], str) and forms[i].strip() and parse_iso(accepted[i]) is not None]


# --------------------------------------------------------------------------- XML


_DTD = re.compile(rb"<!(DOCTYPE|ENTITY)", re.IGNORECASE)


def safe_xml_root(body: bytes) -> ET.Element:
    """Parse a feed body: BOM and leading blanks stripped; any DTD or entity declaration refused
    (no entity expansion); SchemaError when unparseable."""
    if not isinstance(body, (bytes, bytearray)):
        raise SchemaError("feed body is not bytes")
    data = bytes(body).lstrip(b"\xef\xbb\xbf").lstrip()
    if not data:
        raise SchemaError("empty feed body")
    if _DTD.search(data):
        raise SchemaError("feed declares a DTD or entities")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise SchemaError(f"feed is not well-formed XML ({exc.code})") from exc


# --------------------------------------------------------------------------- getcurrent Atom

ATOM = "{http://www.w3.org/2005/Atom}"
_TITLE = re.compile(r"^(?P<form>.+?) - (?P<company>.+) \((?P<cik>\d{1,10})\) \((?P<role>[^()]+)\)\s*$")
_ITEM = re.compile(r"\bItem (\d{1,2}\.\d{2})\b")
_FILED = re.compile(r"Filed:\s*(?:</b>)?\s*(\d{4}-\d{2}-\d{2})")
_ACCNO = re.compile(r"(\d{10}-\d{2}-\d{6})")
_SKIP_ROLES = frozenset({"reporting", "filed by"})  # not the company the filing is about


@dataclass(frozen=True)
class AtomEntry:
    guid: str  # "<accession>|<cik>" (one accession can name a filer and a subject company)
    form: str
    title: str
    cik: int
    company: str
    accepted_at: str  # UTC "...Z" (the entry's <updated>, EDGAR's acceptance time)
    filed_on: str | None
    items: tuple[str, ...]
    link: str | None
    accession: str
    role: str
    summary: str  # tag-stripped summary text (the "description" for filing_class)
    raw: bytes = b""  # the entry as received (re-serialised), for the raw store


@dataclass(frozen=True)
class AtomPage:
    updated: str | None
    entries: tuple[AtomEntry, ...]
    total: int = 0  # entries on the page, the left-out roles included (for the overlap rule)


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html.unescape(text))).strip()


def parse_getcurrent_atom(body: bytes) -> AtomPage:
    """EDGAR's `action=getcurrent ... output=atom` page. Every entry must have the documented shape
    (title "FORM - COMPANY (CIK) (Role)", an id carrying the accession, an <updated> time with its
    offset); any drift is a SchemaError for the whole page. Entries whose role is "Reporting" or
    "Filed by" are left out (they name an insider or agent, not the company)."""
    root = safe_xml_root(body)
    if root.tag != f"{ATOM}feed":
        raise SchemaError(f"getcurrent: root is {root.tag!r}, not an Atom feed")
    updated = parse_iso(_text(root.find(f"{ATOM}updated")))
    entries: list[AtomEntry] = []
    elements = root.findall(f"{ATOM}entry")
    for el in elements:
        title = _text(el.find(f"{ATOM}title"))
        m = _TITLE.match(title)
        if not m:
            raise SchemaError("getcurrent: an entry title has an unknown shape")
        at = parse_iso(_text(el.find(f"{ATOM}updated")))
        if at is None:
            raise SchemaError("getcurrent: an entry has no readable <updated> time")
        ident = _text(el.find(f"{ATOM}id"))
        summary_raw = _text(el.find(f"{ATOM}summary"))
        acc = _ACCNO.search(ident) or _ACCNO.search(summary_raw)
        if acc is None:
            raise SchemaError("getcurrent: an entry carries no accession number")
        category = el.find(f"{ATOM}category")
        form = (category.get("term") if category is not None else None) or m.group("form")
        if not form.strip():
            raise SchemaError("getcurrent: an entry has no form")
        role = m.group("role").strip()
        if role.lower() in _SKIP_ROLES:
            continue
        link_el = el.find(f"{ATOM}link")
        link = link_el.get("href") if link_el is not None else None
        filed = _FILED.search(html.unescape(summary_raw))
        cik = int(m.group("cik"))
        entries.append(AtomEntry(
            guid=f"{acc.group(1)}|{cik}", form=form.strip().upper(), title=title, cik=cik,
            company=m.group("company").strip(), accepted_at=iso(at), filed_on=filed.group(1) if filed else None,
            items=tuple(dict.fromkeys(_ITEM.findall(_strip_tags(summary_raw)))), link=link,
            accession=acc.group(1), role=role, summary=_strip_tags(summary_raw), raw=ET.tostring(el)))
    return AtomPage(updated=iso(updated) if updated else None, entries=tuple(entries), total=len(elements))


# --------------------------------------------------------------------------- SS 6.4 filing table

_OFFERING_FORMS = ("S-1", "S-3", "F-1", "F-3")


def filing_class(form: str, items: Sequence[str] = (), description: str = "") -> str | None:
    """The SS 6.4 filing table. An amendment ("/A") classes as its base form. When an 8-K carries
    several items, the first class in this order wins: offering (3.02), reverse_split (5.03 with
    "reverse" in the description), listing_notice (3.01), earnings_result (2.02),
    contract_or_other (1.01, 2.01, 7.01, 8.01): the same order as the wire classes, where the
    classes that never pass gate 6 come first. None when no row applies."""
    f = (form or "").strip().upper()
    if f.endswith("/A"):
        f = f[:-2]
    reverse = "reverse" in (description or "").lower()
    if f in ("NT 10-Q", "NT 10-K"):
        return "late_filing"
    if f.startswith(_OFFERING_FORMS) or f.startswith("424B") or f == "EFFECT":
        return "offering"
    if f == "DEF 14A":
        return "reverse_split" if reverse else None
    if f != "8-K":
        return None
    have = {str(i).strip() for i in items}
    if "3.02" in have:
        return "offering"
    if "5.03" in have and reverse:
        return "reverse_split"
    if "3.01" in have:
        return "listing_notice"
    if "2.02" in have:
        return "earnings_result"
    if have & {"1.01", "2.01", "7.01", "8.01"}:
        return "contract_or_other"
    return None
