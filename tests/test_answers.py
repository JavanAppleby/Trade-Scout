"""scout.answers (AnswersJob) and scout.jobs_answers (SCOUT_SPEC 10.2 answers, 11; DESIGN 2.5, 5.5, 8)."""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from scout.answers import GATES, AnswersJob, answer_from_results
from scout.app import AppContext
from scout.clock import ManualClock, iso
from scout.config import ScoutConfig, config_from_dict
from scout.heartbeat import SourceStatusBoard
from scout.jobs_answers import LazySubmissions, build_jobs
from scout.models import AnswerRecord, DueWindowRecord, PeriodicInfo
from scout.paths import ScoutPaths
from scout.store import read_jsonl

from conftest import TEST_EMAIL, et_at
from helpers_answers import fact, good_world, install_all_stubs, load_submissions, write_beats
from helpers_fakes import FakeSubmissions, write_records

DAY = "2026-11-10"  # Tuesday; ACME's last 10-Q (period 2026-09-30) was filed 2026-11-09
S = "ACME"


@pytest.fixture(autouse=True)
def _stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    install_all_stubs(monkeypatch)


def _info(ticker: str = S) -> PeriodicInfo:
    info, _raw = load_submissions("submissions_calendar_year_SYNTHETIC.json")
    return dataclasses.replace(info, ticker=ticker, fetched_at="2026-11-10T23:00:00Z")


def _subs(*tickers: str) -> FakeSubmissions:
    return FakeSubmissions({t: _info(t) for t in tickers})


@pytest.fixture
def bots(tmp_path: Path) -> tuple[Path, Path]:
    radar, pilot = tmp_path / "Radar", tmp_path / "Pilot"
    (radar / "state" / "handoff").mkdir(parents=True)
    (pilot / "state" / "plans").mkdir(parents=True)
    return radar, pilot


@pytest.fixture
def cfg2(scout_dir: Path, bots: tuple[Path, Path]) -> ScoutConfig:
    radar, pilot = bots
    return config_from_dict({"contact_email": TEST_EMAIL, "radar_dir": str(radar), "pilot_dir": str(pilot),
                             "watch_symbols": ["WTCH"]}, scout_dir)


def _find(radar: Path, day: str, symbol: str) -> None:
    with open(radar / "state" / "handoff" / f"{day}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"format": 1, "candidate_id": f"c-{symbol}", "symbol": symbol, "found_at": f"{day}T15:00:00Z",
                             "price": 3.2}) + "\n")


def _plan(pilot: Path, symbol: str, status: str = "open") -> None:
    (pilot / "state" / "plans" / f"{symbol}.json").write_text(json.dumps({"symbol": symbol, "status": status}),
                                                             encoding="utf-8")


def _job(cfg: ScoutConfig, subs: Any, now: datetime) -> AnswersJob:
    return AnswersJob(cfg, ScoutPaths.from_config(cfg), subs, ManualClock(now))


# --------------------------------------------------------------------------- due records


def test_refresh_due_writes_one_record_per_symbol(scout_dir: Path, cfg: ScoutConfig) -> None:
    now = et_at(DAY, "19:30")
    subs = _subs(S)
    recs = _job(cfg, subs, now).refresh_due(["acme", "ACME", " nocik ", ""], now)
    assert [r.ticker for r in recs] == [S, "NOCIK"] and subs.calls == [S, "NOCIK"]
    rows = read_jsonl(scout_dir / "state" / "edgar" / "due" / f"{DAY}.jsonl")
    assert [DueWindowRecord.from_record(r).status for r in rows] == ["ok", "no_cik"]
    assert rows[0]["computed_at"] == iso(now) and rows[0]["due_start"] == "2027-01-10"


def test_submissions_failures_fail_closed(scout_dir: Path, cfg: ScoutConfig, caplog: pytest.LogCaptureFixture) -> None:
    class Broken:
        def periodic_info(self, ticker: str, now: datetime) -> Any:
            if ticker == "RAISE":
                raise RuntimeError(f"contact {TEST_EMAIL} failed")
            return {"not": "info"}
    now = et_at(DAY, "19:30")
    with caplog.at_level(logging.WARNING):
        recs = _job(cfg, Broken(), now).refresh_due(["RAISE", "ODD"], now)
    assert [r.status for r in recs] == ["fetch_failed", "fetch_failed"]
    assert TEST_EMAIL not in caplog.text


# --------------------------------------------------------------------------- answers


def test_answer_symbols(scout_dir: Path, cfg: ScoutConfig) -> None:
    now = et_at(DAY, "19:30")
    good_world(scout_dir, cfg, now)
    write_records(scout_dir, "facts", [fact("BETA", published=now - timedelta(hours=1), cls="fda")])
    recs = _job(cfg, _subs(S, "BETA"), now).answer_symbols(["beta", S], now, now, origins={"BETA": "radar"})
    by = {r.symbol: r for r in recs}
    assert (by[S].fresh_catalyst, by[S].event_before_next_session, by[S].news_headline) == ("false", "false", "false")
    assert (by["BETA"].fresh_catalyst, by["BETA"].event_before_next_session) == ("true", "false")
    assert by["BETA"].origin == "radar" and by[S].origin == "manual"
    assert by[S].macro_event_next_session == "false" and by[S].halt_open == "false" and by[S].stale is False
    assert set(by[S].reasons) == set(GATES) == set(by[S].evidence)
    assert by[S].as_of == iso(now) and by[S].written_at == iso(now)


def test_stale_scout_answers_unknown(scout_dir: Path, cfg: ScoutConfig) -> None:
    now = et_at(DAY, "19:30")
    rec = _job(cfg, _subs(S), now).answer_symbols([S], now, now)[0]
    assert rec.stale is True and {rec.fresh_catalyst, rec.event_before_next_session, rec.news_headline} == {"unknown"}


def test_answer_from_results_tolerates_junk() -> None:
    now = et_at(DAY, "19:30")
    rec = answer_from_results(S, now, now, "watch", {"fresh_catalyst": {"value": "maybe", "stale": True},
                                                      "halt_open": "true"})
    assert rec.fresh_catalyst == "unknown" and rec.halt_open == "unknown" and rec.stale is True
    assert AnswerRecord.from_record(rec.to_record()) == rec


def test_run_pilot_covers_pilot_radar_and_watch(scout_dir: Path, cfg2: ScoutConfig, bots: tuple[Path, Path],
                                                write_audit: Any) -> None:
    radar, pilot = bots
    _find(radar, DAY, "RDR")
    _plan(pilot, "PLT")
    _plan(pilot, "SHUT", status="closed")
    _find(radar, DAY, "PLT")  # a Pilot position found again by Radar stays "pilot"
    now = et_at(DAY, "19:30")
    good_world(scout_dir, cfg2, now)
    subs = _subs("RDR", "PLT", "WTCH")
    write_audit.start(scout_dir)
    recs = _job(cfg2, subs, now).run_pilot(now)
    assert {r.symbol: r.origin for r in recs} == {"PLT": "pilot", "RDR": "radar", "WTCH": "watch"}
    assert sorted(subs.calls) == ["PLT", "RDR", "WTCH"]
    stored = [AnswerRecord.from_record(r) for r in read_jsonl(scout_dir / "state" / "answers" / f"{DAY}.jsonl")]
    assert stored == recs and all(r.event_before_next_session == "false" for r in stored)
    assert not any(str(radar) in path or str(pilot) in path for _e, path in write_audit.events)


def test_run_radar_answers_as_of_1945_and_sees_only_earlier_due_records(scout_dir: Path, cfg2: ScoutConfig,
                                                                       bots: tuple[Path, Path]) -> None:
    radar, _pilot = bots
    _find(radar, DAY, "EARLY")
    t1930, t1945, t2001 = et_at(DAY, "19:30"), et_at(DAY, "19:45"), et_at(DAY, "20:01")
    good_world(scout_dir, cfg2, t1945, since=et_at(DAY, "15:00"))
    write_beats(scout_dir, t1930 - timedelta(seconds=60), t1930)
    job = _job(cfg2, _subs("EARLY", "LATE", "WTCH"), t1930)
    job.run_pilot(t1930)  # covers today's finds so far
    _find(radar, DAY, "LATE")  # found after the 19:30 run
    recs = {r.symbol: r for r in job.run_radar(t2001)}
    assert set(recs) == {"EARLY", "LATE"} and {r.as_of for r in recs.values()} == {iso(t1945)}
    assert {r.origin for r in recs.values()} == {"radar"} and recs["EARLY"].written_at == iso(t2001)
    assert recs["EARLY"].event_before_next_session == "false"
    assert recs["LATE"].event_before_next_session == "unknown"  # its due record was computed after 19:45
    assert recs["LATE"].reasons["event_before_next_session"] == ["no_due_record"]
    due = read_jsonl(scout_dir / "state" / "edgar" / "due" / f"{DAY}.jsonl")
    assert sorted(r["ticker"] for r in due if r["computed_at"] == iso(t2001)) == ["EARLY", "LATE"]


def test_early_close_times(scout_dir: Path, cfg: ScoutConfig) -> None:
    job = _job(cfg, _subs(), et_at("2026-11-27", "12:00"))
    day = et_at("2026-11-27", "12:00").date()
    assert job.radar_as_of(day) == et_at("2026-11-27", "16:45")
    assert job.answer_times(day) == [et_at("2026-11-27", "16:30"), et_at("2026-11-27", "16:45")]
    assert job.radar_after(day) == et_at("2026-11-27", "17:00")
    normal = et_at("2026-11-25", "12:00").date()
    assert job.radar_as_of(normal) == et_at("2026-11-25", "19:45")


def test_stored_answers_replay_identically(scout_dir: Path, cfg: ScoutConfig) -> None:
    now = et_at(DAY, "19:45")
    good_world(scout_dir, cfg, now)
    write_records(scout_dir, "facts", [fact(S, published=now - timedelta(hours=2), cls="contract")])
    job = _job(cfg, _subs(S, "BETA"), now)
    stored = job.answer_symbols([S, "BETA"], now, now)
    job.write(stored)
    later = now + timedelta(minutes=20)
    write_records(scout_dir, "facts", [fact("BETA", published=now - timedelta(minutes=1), seen=later, cls="fda")])
    write_beats(scout_dir, later, later, stopped=True)
    replayed = job.answer_symbols([S, "BETA"], now, later, refresh=False)
    strip = [dataclasses.replace(r, written_at="") for r in replayed]
    assert strip == [dataclasses.replace(r, written_at="") for r in stored]


_label = st.dictionaries(
    st.sampled_from(["ticker", "catalyst_type", "direction", "scheduled_event", "status", "verified", "labelled_at",
                     "fact_id", "format", "summary", "evidence_ids"]),
    st.one_of(st.just(S), st.just("earnings"), st.just("2026-11-10T20:00:00Z"), st.just(True), st.integers(),
              st.none(), st.lists(st.text(max_size=5), max_size=2), st.dictionaries(st.text(max_size=3),
                                                                                     st.text(max_size=3), max_size=2)),
    max_size=8)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(labels=st.lists(_label, max_size=4), raw=st.binary(max_size=80))
def test_property_no_label_changes_any_answer(scout_dir: Path, cfg: ScoutConfig, labels: list[dict],
                                              raw: bytes) -> None:
    now = et_at(DAY, "19:45")
    folder = scout_dir / "state" / "labels"
    for p in folder.glob("*"):
        p.unlink()
    if not (scout_dir / "state" / "params.jsonl").exists():
        good_world(scout_dir, cfg, now)
        write_records(scout_dir, "facts", [fact(S, published=now - timedelta(hours=2), cls="other")])
    job = _job(cfg, _subs(S), now)
    before = job.answer_symbols([S], now, now, refresh=False)
    for lab in labels:
        with open(folder / f"{DAY}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"format": 1, "labelled_at": "2026-11-10T20:00:00Z", **lab}) + "\n")
    with open(folder / f"{DAY}.jsonl", "ab") as fh:
        fh.write(raw)
    assert job.answer_symbols([S], now, now, refresh=False) == before


# --------------------------------------------------------------------------- jobs


def _ctx(cfg: ScoutConfig, now: datetime) -> AppContext:
    return AppContext(cfg=cfg, paths=ScoutPaths.from_config(cfg), clock=ManualClock(now), board=SourceStatusBoard(cfg),
                      log=logging.getLogger("test.answers"))


def test_build_jobs_names_and_times(cfg: ScoutConfig) -> None:
    ctx = _ctx(cfg, et_at(DAY, "12:00"))
    jobs = build_jobs(ctx)
    ctx.live_beat_at = et_at(DAY, "12:00")
    assert [j.name for j in jobs] == ["answers_pilot_1930", "answers_pilot_1945", "answers_radar"]
    day = et_at("2026-11-27", "12:00").date()
    want = [et_at("2026-11-27", t) for t in ("16:30", "16:45", "17:00")]
    assert [j.at_on(day) for j in jobs] == want  # type: ignore[attr-defined]
    tuesday = et_at(DAY, "12:00").date()
    assert jobs[2].at_on(tuesday) == et_at(DAY, "20:00")  # type: ignore[attr-defined]
    assert not jobs[0].due(et_at(DAY, "19:29")) and jobs[0].due(et_at(DAY, "19:30"))
    assert not jobs[0].due(et_at("2026-11-14", "19:30"))  # Saturday


def test_jobs_run_without_the_wire_unit(scout_dir: Path) -> None:
    now = et_at(DAY, "19:30")
    cfg = config_from_dict({"contact_email": TEST_EMAIL, "watch_symbols": [S]}, scout_dir)
    ctx = _ctx(cfg, now)

    def no_wire() -> Any:
        raise ModuleNotFoundError("No module named 'scout.edgar_submissions'", name="scout.edgar_submissions")
    ctx.submissions = no_wire  # type: ignore[method-assign]
    assert LazySubmissions(ctx).periodic_info("acme", now).status == "fetch_failed"
    good_world(scout_dir, cfg, now)
    jobs = build_jobs(ctx)
    jobs[0].run(now)
    rows = read_jsonl(scout_dir / "state" / "answers" / f"{DAY}.jsonl")
    assert [r["symbol"] for r in rows] == [S] and rows[0]["event_before_next_session"] == "unknown"
    assert rows[0]["reasons"]["event_before_next_session"] == ["due_status:fetch_failed"]


def test_lazy_submissions_uses_the_built_source(cfg: ScoutConfig) -> None:
    now = et_at(DAY, "19:30")
    ctx = _ctx(cfg, now)
    subs = _subs(S)
    ctx.submissions = lambda: subs  # type: ignore[method-assign]
    assert LazySubmissions(ctx).periodic_info(S, now).status == "ok" and subs.calls == [S]
