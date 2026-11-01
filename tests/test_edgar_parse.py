"""scout.edgar_parse (SCOUT_SPEC 4.1 #1-3, 6.3, 6.4): Radar's copy gives Radar's results on a shared
fixture; the periodic and getcurrent parsers; schema drift is a SchemaError, never skipped rows."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from helpers_wire import fixture, fixture_json

from scout import edgar_parse as EP
from scout.edgar_parse import (
    RadarConfigAdapter, SchemaError, flags_from_filings, parse_getcurrent_atom, parse_json, parse_periodic,
    parse_submissions, parse_ticker_map, safe_xml_root,
)

SHARED = fixture_json("radar_shared_submissions.json")
RADAR_SHA = "379d46e1db88f1091f9890f9b0a6be9f479d354ab1fa751e6c57e7d6691b03c3"


# --------------------------------------------------------------------------- Radar's copy


def test_provenance_header_names_commit_and_sha():
    doc = EP.__doc__ or ""
    assert "6c971c8a6bda0c68ed6a8ac261e1cd96ed65eade" in doc and RADAR_SHA in doc
    assert RADAR_SHA in SHARED["_provenance"]


def test_parse_submissions_matches_radar():
    got = [[f.form, f.accepted.isoformat(), f.filing_date.isoformat(), f.accession, list(f.items)]
           for f in parse_submissions(SHARED["submissions"])]
    assert got == SHARED["expected"]["parse_submissions"]


def test_flags_from_filings_matches_radar():
    filings = parse_submissions(SHARED["submissions"])
    for at, (news, dilution, evidence) in SHARED["expected"]["flags"].items():
        flags = flags_from_filings(filings, EP.parse_iso(at), RadarConfigAdapter())
        assert (flags.news_filing, flags.dilution_risk, list(flags.evidence)) == (news, dilution, evidence), at


def test_adapter_carries_radar_defaults():
    want = SHARED["expected"]["radar_config_defaults"]
    adapter = RadarConfigAdapter()
    for key, value in want.items():
        got = getattr(adapter, key)
        assert (list(got) if isinstance(got, tuple) else got) == value, key


def test_parse_ticker_map_matches_radar():
    got = {k: list(v) for k, v in parse_ticker_map(SHARED["ticker_map_body"]).items()}
    assert got == SHARED["expected"]["ticker_map"]


# --------------------------------------------------------------------------- periodic


def test_parse_periodic_calendar_filer():
    filings, fye = parse_periodic(fixture("submissions_calendar_filer.json"))
    assert fye == "1231"
    assert [(f.form, f.report_date) for f in filings] == [
        ("8-K", "2026-08-06"), ("10-Q", "2026-06-30"), ("10-Q", "2026-03-31"), ("10-K", "2025-12-31"),
        ("S-3", None), ("10-Q", "2025-09-30")]
    first = filings[0]
    assert first.items == ("2.02", "9.01") and first.accepted == "2026-08-06T20:05:12Z"
    assert first.filing_date == "2026-08-06" and first.accession == "0001234567-26-000900"


def test_parse_periodic_june_filer_with_nt_rows_and_a_bad_row():
    filings, fye = parse_periodic(fixture("submissions_june_fy_filer.json"))
    assert fye == "0630"
    forms = [f.form for f in filings]
    assert forms == ["8-K", "10-K", "NT 10-K", "10-Q", "NT 10-Q", "10-Q"]  # the "garbage" time row is left out
    assert filings[-1].report_date == "2025-12-31"  # report dates stay aligned after the skipped row


def test_parse_periodic_aligns_with_radar_rows():
    body = SHARED["submissions"]
    mine, _ = parse_periodic(json.dumps(body).encode())
    radar = parse_submissions(body)
    assert [(f.form, f.accession) for f in mine] == [(f.form, f.accession) for f in radar]


@pytest.mark.parametrize("body", [
    b"not json", b"\xff\xfe", b"[]", b"{}", b'{"filings": {}}', b'{"filings": {"recent": []}}',
    b'{"filings": {"recent": {"form": ["8-K"]}}}',
    b'{"filings": {"recent": {"form": ["8-K"], "acceptanceDateTime": [], "accessionNumber": [], "filingDate": []}}}',
])
def test_parse_periodic_schema_drift(body):
    with pytest.raises(SchemaError):
        parse_periodic(body)


@pytest.mark.parametrize("fye, want", [("1231", "1231"), ("0630", "0630"), ("1332", None), ("12-31", None),
                                       (None, None), (1231, None)])
def test_fiscal_year_end_must_be_mmdd(fye, want):
    body = fixture_json("submissions_calendar_filer.json")
    body["fiscalYearEnd"] = fye
    assert parse_periodic(body)[1] == want


def test_parse_json():
    assert parse_json(b"\xef\xbb\xbf{\"a\": 1}") == {"a": 1}
    assert parse_json({"a": 1}) == {"a": 1}
    with pytest.raises(SchemaError):
        parse_json("{")


# --------------------------------------------------------------------------- getcurrent Atom


def test_getcurrent_8k_page():
    page = parse_getcurrent_atom(fixture("edgar_getcurrent_8k.atom"))
    assert page.total == 7 and len(page.entries) == 6  # the "Reporting" entry is left out
    assert page.updated == "2026-11-02T14:05:00Z"
    acme = page.entries[0]
    assert (acme.form, acme.cik, acme.company, acme.role) == ("8-K", 1234567, "ACME CORP", "Filer")
    assert acme.items == ("2.02", "9.01") and acme.accepted_at == "2026-11-02T13:31:12Z"
    assert acme.filed_on == "2026-11-02" and acme.accession == "0001234567-26-000101"
    assert acme.guid == "0001234567-26-000101|1234567"
    assert acme.link.startswith("https://www.sec.gov/Archives/edgar/data/1234567/")
    assert b"accession-number=0001234567-26-000101" in acme.raw
    forms = [e.form for e in page.entries]
    assert forms[-1] == "8-K/A" and page.entries[-1].items == ("5.03",)
    subject = [e for e in page.entries if e.role == "Subject"]
    assert len(subject) == 1 and subject[0].cik == 1652044


def test_getcurrent_424b_page_keeps_every_424b_form():
    """D6 (UNVERIFIED live): `type=424B` is assumed to return 424B1-424B8; synthetic fixture."""
    page = parse_getcurrent_atom(fixture("edgar_getcurrent_424b.atom"))
    assert [e.form for e in page.entries] == ["424B5", "424B3", "424B4"]
    assert all(e.items == () for e in page.entries)


def test_getcurrent_empty_page():
    page = parse_getcurrent_atom(fixture("edgar_getcurrent_empty.atom"))
    assert page.entries == () and page.total == 0


def _mutate(old: bytes, new: bytes) -> bytes:
    body = fixture("edgar_getcurrent_8k.atom")
    assert old in body
    return body.replace(old, new, 1)


@pytest.mark.parametrize("body", [
    _mutate(b"<title>8-K - ACME CORP (0001234567) (Filer)</title>", b"<title>ACME filed something</title>"),
    _mutate(b"<updated>2026-11-02T08:31:12-05:00</updated>", b"<updated>soon</updated>"),
    _mutate(b"<updated>2026-11-02T08:31:12-05:00</updated>", b""),
    _mutate(b"<id>urn:tag:sec.gov,2008:accession-number=0001234567-26-000101</id>", b"<id>x</id>")
    .replace(b"AccNo:&lt;/b&gt; 0001234567-26-000101", b"AccNo:&lt;/b&gt; none", 1),
    _mutate(b'<feed xmlns="http://www.w3.org/2005/Atom">', b"<feed>"),
    b"<html><body>Your request was blocked</body></html>",
    b"",
    b"<feed",
    b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><feed xmlns="http://www.w3.org/2005/Atom"/>',
])
def test_getcurrent_schema_drift_refuses_the_page(body):
    with pytest.raises(SchemaError):
        parse_getcurrent_atom(body)


def test_safe_xml_root_strips_bom_and_refuses_non_bytes():
    assert safe_xml_root(b"\xef\xbb\xbf  <a/>").tag == "a"
    with pytest.raises(SchemaError):
        safe_xml_root("<a/>")  # type: ignore[arg-type]


def test_module_is_pure_and_small():
    import ast

    src = Path(EP.__file__).read_text(encoding="utf-8")
    imported = {n.names[0].name.split(".")[0] if isinstance(n, ast.Import) else (n.module or "").split(".")[0]
                for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.Import, ast.ImportFrom))}
    assert not imported & {"urllib", "socket", "http", "trade_guardian", "radar", "webull"}
    assert len(src.splitlines()) < 600


def test_filing_accepted_is_utc():
    filings, _ = parse_periodic(fixture("submissions_calendar_filer.json"))
    assert all(datetime.fromisoformat(f.accepted.replace("Z", "+00:00")).tzinfo == timezone.utc for f in filings)
