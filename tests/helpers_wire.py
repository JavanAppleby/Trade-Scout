"""Shared helpers for the wire unit's tests: fixtures, URLs and a fake transport for every feed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import TEST_EMAIL
from helpers_fakes import FakeResponse, FakeTransport

from scout.config import ScoutConfig, config_from_dict
from scout.edgar_atom import form_url

WIRE = Path(__file__).parent / "fixtures" / "wire"
FORMS = ["8-K", "424B", "NT 10-K"]  # a short form list keeps the tests readable
# An invented second gate-6 wire (GlobeNewswire was removed on 2026-11-03): tests of category tickers,
# 403 refusal or of two gate-6 wires add it with this override. Never fetched for real.
TEST_WIRE_URL = "https://wire.example.test/rss"
TEST_WIRE: dict[str, Any] = {"testwire": {
    "enabled": True, "kind": "wire", "url": TEST_WIRE_URL, "paths": [], "min_spacing_s": 60.0,
    "idle_spacing_s": 60.0, "allowed_hosts": ["wire.example.test"], "terms_note": "test only", "max_items": 20,
    "gate6": True, "terms_ok": False}}


def fixture(name: str) -> bytes:
    return (WIRE / name).read_bytes()


def fixture_json(name: str) -> Any:
    return json.loads((WIRE / name).read_text(encoding="utf-8"))


def wire_config(scout_dir: Path, **overrides: Any) -> ScoutConfig:
    data: dict[str, Any] = {"contact_email": TEST_EMAIL, "edgar_atom_forms": FORMS}
    data.update(overrides)
    return config_from_dict(data, scout_dir)


def atom_url(cfg: ScoutConfig, form: str) -> str:
    return form_url(cfg.source("edgar_atom").url, form)


def atom_fixture_for(form: str) -> str:
    return {"8-K": "edgar_getcurrent_8k.atom", "424B": "edgar_getcurrent_424b.atom"}.get(
        form, "edgar_getcurrent_empty.atom")


def ok(body: bytes, **headers: str) -> FakeResponse:
    return FakeResponse(200, dict(headers), body)


def full_transport(cfg: ScoutConfig) -> FakeTransport:
    """Every wire-unit URL answered from its fixture."""
    responses: dict[str, Any] = {
        cfg.source("edgar_tickers").url: ok(fixture("company_tickers_exchange_excerpt.json")),
        cfg.source("prnewswire").url: ok(fixture("prnewswire.rss")),
        cfg.source("stocktitan").url: ok(fixture("stocktitan.rss")),
        cfg.source("edgar_submissions").url.format(cik=1234567): ok(fixture("submissions_calendar_filer.json")),
        cfg.source("edgar_submissions").url.format(cik=7654321): ok(fixture("submissions_june_fy_filer.json")),
    }
    if "testwire" in cfg.sources:
        responses[TEST_WIRE_URL] = ok(fixture("category_wire.rss"))
    for form in cfg.edgar_atom_forms:
        responses[atom_url(cfg, form)] = ok(fixture(atom_fixture_for(form)))
    return FakeTransport(responses)
