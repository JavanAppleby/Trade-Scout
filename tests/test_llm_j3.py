"""J3 research with server web tools (SCOUT_SPEC 8.2, 8.4: a cited URL not in the tool results is
rejected)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from conftest import make_config
from helpers_fake_anthropic import (
    FakeAnthropic, fact, fetch_result, label, message, search_result, server_tool_use, text, usage,
)
from helpers_fakes import write_records
from scout.clock import ManualClock
from scout.config import ScoutConfig
from scout.llm_checks import check_j3, host_allowed
from scout.llm_j3 import J3Job
from scout.llm_ledger import Ledger
from scout.llm_records import calls_on, labels_on
from scout.paths import ScoutPaths

DAY = date(2026, 11, 2)
URL = "https://www.globenewswire.com/news-release/2026/11/02/xyz-award.html"
PAGE = "XYZ Holdings (NASDAQ: XYZ) today announced a $12 million supply contract with the US Army."
QUOTE = "today announced a $12 million supply contract with the US Army"


@pytest.fixture
def llm_cfg(scout_dir: Path) -> ScoutConfig:
    return make_config(scout_dir, llm_enabled=True)


@pytest.fixture
def lpaths(llm_cfg: ScoutConfig) -> ScoutPaths:
    return ScoutPaths.from_config(llm_cfg)


def reply(citations: list[dict[str, str]], *, urls: tuple[str, ...] = (URL,), fetched: dict[str, str] | None = None,
          lab: dict[str, Any] | None = None) -> Any:
    blocks: list[Any] = [server_tool_use("web_search", "s1", query="XYZ news"), search_result("s1", urls)]
    for n, (u, page) in enumerate((fetched if fetched is not None else {URL: PAGE}).items()):
        blocks += [server_tool_use("web_fetch", f"f{n}", url=u), fetch_result(f"f{n}", u, page)]
    blocks.append(text({"label": lab or label("XYZ", []), "citations": citations}))
    return message(blocks, model="claude-sonnet-5-5", use=usage(3000, 400, searches=1, fetches=len(blocks) // 2 - 1))


def _research(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock, *replies: Any) -> tuple[dict, FakeAnthropic]:
    tr = FakeAnthropic(*replies)
    J3Job(cfg, paths, tr, Ledger(paths, cfg, clock), clock).research("XYZ", clock.now())
    return labels_on(paths, DAY)[-1], tr


def test_verified_with_fetched_citation(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock) -> None:
    lab, tr = _research(llm_cfg, lpaths, clock, reply([{"url": URL, "quote": QUOTE}]))
    assert lab["verified"] is True and lab["check_failures"] == [] and lab["job"] == "J3"
    assert lab["urls"] == [URL] and lab["quotes"] == [QUOTE] and lab["fact_id"] is None
    params = tr.calls[0]
    assert {t["name"] for t in params["tools"]} == {"web_search", "web_fetch"} and "fallbacks" not in params
    rec = calls_on(lpaths, DAY)[0]
    assert rec["usage"]["web_search_requests"] == 1 and rec["cost_usd"] > 0.01  # $10 per 1000 searches


@pytest.mark.parametrize("cite, kw, failure", [
    ({"url": "https://www.globenewswire.com/other", "quote": QUOTE}, {}, "citation0:url_not_in_results"),
    ({"url": "https://evil.example.com/x", "quote": QUOTE},
     {"urls": ("https://evil.example.com/x",), "fetched": {"https://evil.example.com/x": PAGE}},
     "citation0:host_not_allowed"),
    ({"url": URL, "quote": "announced a $99 million contract"}, {}, "citation0:quote_not_in_page"),
    ({"url": URL, "quote": QUOTE}, {"fetched": {}}, "citation0:url_not_fetched"),
    ({"url": URL, "quote": "short"}, {}, "citation0:quote_too_short"),
    ({"url": URL, "quote": QUOTE}, {"fetched": {URL: "Someone " + QUOTE}}, "citation0:symbol_not_named"),
])
def test_citation_checks(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock, cite: dict, kw: dict,
                         failure: str) -> None:
    lab, _ = _research(llm_cfg, lpaths, clock, reply([cite], **kw))
    assert lab["verified"] is False and failure in lab["check_failures"]


def test_no_citations_never_verified(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock) -> None:
    lab, _ = _research(llm_cfg, lpaths, clock, reply([], lab=label("XYZ", [], catalyst_type="none")))
    assert lab["status"] == "labelled" and lab["verified"] is False and lab["check_failures"] == ["no_citations"]


def test_company_name_counts(llm_cfg: ScoutConfig) -> None:
    blocks = [{"type": "web_fetch_tool_result", "content": {
        "type": "web_fetch_result", "url": URL,
        "content": {"type": "document", "source": {"type": "text", "data": "Xyz Holdings Inc " + QUOTE}}}}]
    assert check_j3({"ticker": "XYZ"}, [{"url": URL, "quote": QUOTE}], blocks, llm_cfg.llm_allowed_domains,
                    company="XYZ Holdings") == []
    assert host_allowed("https://www.sec.gov/x", ["sec.gov"]) and not host_allowed("https://notsec.gov/x", ["sec.gov"])
    assert not host_allowed("ftp://www.sec.gov/x", ["sec.gov"])


def _pilot(tmp_path: Path, *symbols: str) -> Path:
    plans = tmp_path / "Pilot" / "state" / "plans"
    plans.mkdir(parents=True)
    for s in symbols:
        (plans / f"{s}.json").write_text(json.dumps({"symbol": s, "status": "open"}), encoding="utf-8")
    return tmp_path / "Pilot"


def test_positions_once_a_day_and_cap(scout_dir: Path, clock: ManualClock, tmp_path: Path) -> None:
    cfg = make_config(scout_dir, llm_enabled=True, pilot_dir=str(_pilot(tmp_path, "XYZ", "ABC")), j3_max_calls_per_day=1)
    paths = ScoutPaths.from_config(cfg)
    tr = FakeAnthropic(reply([{"url": URL, "quote": QUOTE}], lab=label("ABC", [])), reply([]))
    job = J3Job(cfg, paths, tr, Ledger(paths, cfg, clock), clock)
    assert job.run_positions(clock.now()) == 1  # ABC researched; XYZ stopped by j3_max_calls_per_day
    assert len(tr.calls) == 1
    assert job.run_positions(clock.now()) == 0 and len(tr.calls) == 1


def test_gap_fill(scout_dir: Path, clock: ManualClock, tmp_path: Path) -> None:
    finds = tmp_path / "Radar" / "state" / "finds"
    finds.mkdir(parents=True)
    (finds / "2026-11-02.jsonl").write_text(
        json.dumps({"candidate_id": "c1", "symbol": "XYZ", "found_at": "2026-11-02T14:00:00Z", "price": 4.0}) + "\n"
        + json.dumps({"candidate_id": "c2", "symbol": "NEWS", "found_at": "2026-11-02T14:00:00Z", "price": 2.0}) + "\n",
        encoding="utf-8")
    cfg = make_config(scout_dir, llm_enabled=True, radar_dir=str(tmp_path / "Radar"))
    paths = ScoutPaths.from_config(cfg)
    write_records(scout_dir, "facts", [fact("NEWS", "NEWS Corp wins order", "2026-11-02T14:05:00Z")])
    tr = FakeAnthropic(reply([{"url": URL, "quote": QUOTE}]))
    job = J3Job(cfg, paths, tr, Ledger(paths, cfg, clock), clock)
    assert job.gap_symbols(clock.now()) == []  # not yet 10 minutes after the find
    later = datetime(2026, 11, 2, 14, 11, tzinfo=timezone.utc)
    clock.set(later)
    assert job.gap_symbols(later) == ["XYZ"]
    assert job.run_gap_fill(later) == 1 and job.run_gap_fill(later) == 0 and len(tr.calls) == 1


def test_disabled_does_nothing(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock) -> None:
    tr = FakeAnthropic()
    job = J3Job(cfg, paths, tr, Ledger(paths, cfg, clock), clock)
    assert job.run_positions(clock.now()) == 0 and job.run_gap_fill(clock.now()) == 0 and tr.calls == []


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "llm" / "j3_response.json"


def _fixture_message(citation_url: str | None = None) -> Any:
    from helpers_fake_anthropic import FakeMessage  # noqa: PLC0415

    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert "_SYNTHETIC" in data
    if citation_url is not None:
        final = json.loads(data["content"][-1]["text"])
        final["citations"][0]["url"] = citation_url
        data["content"][-1]["text"] = json.dumps(final)
    msg = FakeMessage(**{k: v for k, v in data.items() if not k.startswith("_")})
    msg._request_id = data["_request_id"]
    return msg


@pytest.mark.parametrize("url, verified, failure", [
    (None, True, None),
    ("https://www.sec.gov/Archives/edgar/data/1/000000000126000001/xyz8k.htm", False, "citation0:url_not_fetched"),
    ("https://www.globenewswire.com/news-release/2026/11/02/made-up.html", False, "citation0:url_not_in_results"),
])
def test_fixture_response(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock, url: str | None,
                          verified: bool, failure: str | None) -> None:
    lab, _ = _research(llm_cfg, lpaths, clock, _fixture_message(url))
    assert lab["verified"] is verified and lab["request_id"] == "req_synthetic_j3"
    assert lab["usage"]["web_fetch_requests"] == 1 and lab["usage"]["cache_creation_input_tokens"] == 1900
    if failure:
        assert failure in lab["check_failures"]


def test_a_date_quote_from_a_page_about_another_company_is_not_verified(llm_cfg: ScoutConfig, lpaths: ScoutPaths,
                                                                        clock: ManualClock) -> None:
    """Regression: J3 checked a date quote against every fetched page, whichever company it was about."""
    other = "https://www.globenewswire.com/news-release/2026/11/02/other.html"
    other_page = "Other Corp (NYSE: OTHR) will report third quarter results on November 10, 2026."
    event = {"kind": "earnings", "date": "2026-11-10", "quote": "will report third quarter results on November 10"}
    lab, _ = _research(llm_cfg, lpaths, clock, reply(
        [{"url": URL, "quote": QUOTE}], urls=(URL, other), fetched={URL: PAGE, other: other_page},
        lab=label("XYZ", [], scheduled_event=event)))
    assert lab["verified"] is False and "event_quote_not_in_source" in lab["check_failures"]
    own = PAGE + " XYZ will report third quarter results on November 10, 2026."
    lab, _ = _research(llm_cfg, lpaths, clock, reply(
        [{"url": URL, "quote": QUOTE}], fetched={URL: own}, lab=label("XYZ", [], scheduled_event=event)))
    assert lab["verified"] is True
