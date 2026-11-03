"""J1 bulk tagging (SCOUT_SPEC 8.2-8.5; DESIGN D8). Mocked client only."""

from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import make_config
from helpers_fake_anthropic import FakeAnthropic, fact, j1_reply, label, message
from helpers_fakes import write_records
from scout.clock import ManualClock
from scout.config import ScoutConfig
from scout.llm_j1 import J1Job, pending_facts
from scout.llm_ledger import Ledger
from scout.llm_records import calls_on, labels_on
from scout.paths import ScoutPaths

T = "2026-11-02T13:00:00Z"
DAY = datetime(2026, 11, 2).date()
READER = Path(__file__).resolve().parent.parent / "scout_reader" / "scout_reader"


@pytest.fixture
def llm_cfg(scout_dir: Path) -> ScoutConfig:
    return make_config(scout_dir, llm_enabled=True)


@pytest.fixture
def lpaths(llm_cfg: ScoutConfig) -> ScoutPaths:
    return ScoutPaths.from_config(llm_cfg)


def _job(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock, *replies: object) -> tuple[J1Job, FakeAnthropic]:
    tr = FakeAnthropic(*replies)
    return J1Job(cfg, paths, tr, Ledger(paths, cfg, clock), clock), tr


def _two_facts(scout_dir: Path) -> None:
    write_records(scout_dir, "facts", [
        fact("ABC", "ABC Corp wins $5M Navy contract", T, fid="fa", url="https://www.prnewswire.com/a"),
        fact("XYZ", "XYZ Inc announces $10M registered direct offering", "2026-11-02T13:05:00Z", fid="fx",
             cls="offering"),
    ])


def test_labels_each_fact(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock, scout_dir: Path,
                          write_audit) -> None:  # noqa: ANN001
    _two_facts(scout_dir)
    job, tr = _job(llm_cfg, lpaths, clock, j1_reply(
        ("F1", label("ABC", ["F1"])),
        ("F2", label("XYZ", ["F2"], catalyst_type="offering_or_dilution", dilution_risk="high", direction="negative"))))
    with write_audit.watching(lpaths.llm, lpaths.labels):
        assert job.run(clock.now()) == 2
    assert len(tr.calls) == 1
    labs = {r["fact_id"]: r for r in labels_on(lpaths, DAY)}
    a, x = labs["fa"], labs["fx"]
    assert a["status"] == "labelled" and a["verified"] is True and a["check_failures"] == []
    assert a["evidence_ids"] == ["fa"] and a["urls"] == ["https://www.prnewswire.com/a"] and a["published_at"] == T
    assert a["model_id"] == "claude-haiku-5-5" and a["request_id"] == "req_fake_1" and a["trust"] == "layer2"
    assert a["schema_version"] == 1 and len(a["prompt_sha256"]) == 64 and a["labelled_at"] == "2026-11-02T14:00:00Z"
    assert x["dilution_risk"] == "high" and x["verified"] is True
    assert calls_on(lpaths, DAY)[0]["fact_ids"] == ["fa", "fx"]
    # nothing new: no second call
    assert job.run(clock.now()) == 0 and len(tr.calls) == 1


def test_missing_item_and_failed_checks(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock,
                                        scout_dir: Path) -> None:
    _two_facts(scout_dir)
    job, _ = _job(llm_cfg, lpaths, clock, j1_reply(("F1", label("NOTIN", ["F1", "F7"]))))
    assert job.run(clock.now()) == 2
    labs = {r["fact_id"]: r for r in labels_on(lpaths, DAY)}
    assert labs["fa"]["status"] == "labelled" and labs["fa"]["verified"] is False
    assert set(labs["fa"]["check_failures"]) == {"ticker_not_in_input", "evidence_id_unknown:F7"}
    assert labs["fx"]["status"] == "unlabelled" and labs["fx"]["check_failures"] == ["no_label_for_item"]
    assert labs["fx"]["ticker"] == "XYZ" and labs["fx"]["catalyst_type"] is None


def test_release_ticker_set(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock, scout_dir: Path) -> None:
    write_records(scout_dir, "facts", [fact("AAA", "AAA and BBB sign merger", T, fid="f1", guid="g"),
                                       fact("BBB", "AAA and BBB sign merger", T, fid="f2", guid="g")])
    job, _ = _job(llm_cfg, lpaths, clock, j1_reply(("F1", label("BBB", ["F1"])), ("F2", label("AAA", ["F2"]))))
    job.run(clock.now())
    assert all(r["verified"] for r in labels_on(lpaths, DAY))  # either ticker of the release may be labelled


def test_schema_failure_twice_gives_unlabelled(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock,
                                               scout_dir: Path) -> None:
    _two_facts(scout_dir)
    job, tr = _job(llm_cfg, lpaths, clock, message({"labels": "nope"}), message("{", stop_reason="max_tokens"))
    assert job.run(clock.now()) == 2 and len(tr.calls) == 2
    labs = labels_on(lpaths, DAY)
    assert {r["status"] for r in labs} == {"unlabelled"} and not any(r["verified"] for r in labs)
    assert labs[0]["check_failures"] == ["call_max_tokens"]
    assert [r["retry"] for r in calls_on(lpaths, DAY)] == [False, True]


@pytest.mark.parametrize("event, ok", [
    ({"kind": "earnings", "date": "2026-11-12", "quote": "ABC Corp wins $5M Navy contract"}, True),
    ({"kind": "earnings", "date": "2026-11-12", "quote": "ABC will report on November 12"}, False),
    ({"kind": "earnings", "date": "2027-08-01", "quote": "ABC Corp wins $5M Navy contract"}, False),
    ({"kind": "earnings", "date": "2026-10-20", "quote": "ABC Corp wins $5M Navy contract"}, False),
    ({"kind": "earnings", "date": "Nov 12", "quote": "ABC Corp wins $5M Navy contract"}, False),
])
def test_scheduled_event_checks(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock, scout_dir: Path,
                                event: dict, ok: bool) -> None:
    write_records(scout_dir, "facts", [fact("ABC", "ABC Corp wins $5M Navy contract", T, fid="fa")])
    job, _ = _job(llm_cfg, lpaths, clock, j1_reply(("F1", label("ABC", ["F1"], scheduled_event=event))))
    job.run(clock.now())
    assert labels_on(lpaths, DAY)[0]["verified"] is ok


def test_not_run_off_session_or_disabled(llm_cfg: ScoutConfig, lpaths: ScoutPaths, cfg: ScoutConfig,
                                         paths: ScoutPaths, clock: ManualClock, scout_dir: Path) -> None:
    _two_facts(scout_dir)
    job, tr = _job(llm_cfg, lpaths, clock)
    night = datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc)  # 21:00 ET: NIGHT
    assert job.run(night) == 0
    off, tr2 = _job(cfg, paths, clock)
    assert off.run(clock.now()) == 0
    assert tr.calls == [] and tr2.calls == []


def test_pending_skips_dear_radar_tickers_and_future(scout_dir: Path, clock: ManualClock, tmp_path: Path) -> None:
    radar = tmp_path / "Radar"
    (radar / "state" / "handoff").mkdir(parents=True)
    (radar / "state" / "handoff" / "2026-11-02.jsonl").write_text(
        json.dumps({"format": 1, "candidate_id": "c1", "symbol": "XYZ", "found_at": T, "price": 45.0}) + "\n"
        + json.dumps({"format": 1, "candidate_id": "c2", "symbol": "ABC", "found_at": T, "price": 3.1}) + "\n", encoding="utf-8")
    cfg = make_config(scout_dir, llm_enabled=True, radar_dir=str(radar))
    paths = ScoutPaths.from_config(cfg)
    _two_facts(scout_dir)
    write_records(scout_dir, "facts", [fact("LATE", "seen later", "2026-11-02T15:00:00Z", fid="fl"),
                                       fact(None, "no ticker", T, fid="fn"),
                                       fact("HLT", "halt", T, fid="fh", kind="halt", cls=None)])
    facts, sets = pending_facts(cfg, paths, clock.now())
    assert [f["fact_id"] for f in facts] == ["fa"] and sets == {"fa": ["ABC"]}


def test_replay_reads_records_only(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock,
                                   scout_dir: Path) -> None:
    _two_facts(scout_dir)
    job, tr = _job(llm_cfg, lpaths, clock, j1_reply(("F1", label("ABC", ["F1"])), ("F2", label("XYZ", ["F2"]))))
    job.run(clock.now())
    first = labels_on(lpaths, DAY)
    tr.replies.clear()
    assert labels_on(lpaths, DAY) == first and len(tr.calls) == 1  # reading back never calls the model


# --------------------------------------------------------------------------- labels never change an answer


def test_reader_never_reads_labels() -> None:
    """Static: no scout_reader module but records.py (the folder-name constants) names the labels folder."""
    for path in READER.glob("*.py"):
        if path.name == "records.py":
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "LABELS" not in names, path.name
        assert "state/labels" not in source and "labels" not in {
            c.value for c in ast.walk(tree) if isinstance(c, ast.Constant) and isinstance(c.value, str)}, path.name


def test_answers_identical_with_and_without_labels(scout_dir: Path, clock: ManualClock, llm_cfg: ScoutConfig) -> None:
    api = pytest.importorskip("scout_reader.api", reason="scout_reader.api is built by the answers unit")
    now = clock.now()
    write_records(scout_dir, "params", [dict(llm_cfg.reader_params(), first_seen_at="2026-11-02T00:00:00Z",
                                             config_sha256="x")])
    write_records(scout_dir, "beats", [{"at": "2026-11-02T13:59:40Z", "stopped": False,
                                        "sources": {s: "ok" for s in llm_cfg.sources}, "gap_open": {}}])
    write_records(scout_dir, "facts", [fact("ABC", "ABC wins contract", T, fid="fa")])

    def answers() -> list:
        r = api.Reader(scout_dir, now=lambda: now)
        return [r.news_headline("ABC", now), r.fresh_catalyst("ABC", now), r.event_before_next_session("ABC", now),
                r.halt_open("ABC", now), r.jason_tags("ABC", 3.0, now), r.market_context(now)]

    before = answers()
    lab = {"job": "J1", "fact_id": "fa", "status": "labelled", "ticker": "ABC", "catalyst_type": "fda_decision",
           "direction": "positive", "dilution_risk": "none", "is_new_information": True,
           "scheduled_event": {"kind": "earnings", "date": "2026-11-02", "quote": "reports tonight after close"},
           "confidence": "high", "evidence_ids": ["fa"], "summary": "x", "verified": True, "check_failures": [],
           "model_id": "m", "prompt_sha256": "p", "schema_version": 1, "request_id": None, "usage": {},
           "cost_usd": 0.0, "labelled_at": "2026-11-02T13:30:00Z", "urls": [], "quotes": [], "published_at": None,
           "trust": "layer2"}
    write_records(scout_dir, "labels", [lab, dict(lab, ticker="ZZZ", catalyst_type="promotion_suspected")])
    assert answers() == before


def test_a_date_quote_from_another_tickers_item_is_not_verified(llm_cfg: ScoutConfig, lpaths: ScoutPaths,
                                                                 clock: ManualClock, scout_dir: Path) -> None:
    """Regression: AAA's label could cite BBB's item and quote BBB's report date; the quote was
    found in BBB's text and AAA was stored verified with BBB's date."""
    write_records(scout_dir, "facts", [
        fact("AAA", "AAA Corp wins $5M Navy contract", T, fid="fa"),
        fact("BBB", "BBB to report results on 2026-11-10", T, fid="fb", cls="earnings_date"),
    ])
    event = {"kind": "earnings", "date": "2026-11-10", "quote": "BBB to report results on 2026-11-10"}
    job, _ = _job(llm_cfg, lpaths, clock, j1_reply(
        ("F1", label("AAA", ["F1", "F2"], scheduled_event=event)), ("F2", label("BBB", ["F2"], scheduled_event=event))))
    job.run(clock.now())
    by = {lab["ticker"]: lab for lab in labels_on(lpaths, DAY)}
    assert by["AAA"]["verified"] is False and "event_quote_not_in_source" in by["AAA"]["check_failures"]
    assert by["BBB"]["verified"] is True  # its own item: still fine
