"""The morning brief (SCOUT_SPEC 10.2 brief, 5.5, 7.3, 8.6; DESIGN 2.6)."""

from __future__ import annotations

import argparse
import ast
import json
import logging
from datetime import date
from pathlib import Path

import pytest

from conftest import make_config
from helpers_fake_anthropic import FakeAnthropic, fact, make_ctx, message
from helpers_fakes import write_records
from scout.brief import SECTIONS, brief_path, build_brief, cli_brief, collect, j4_items, write_brief
from scout.clock import ManualClock
from scout.config import ScoutConfig
from scout.jobs_brief import build_jobs
from scout.paths import ScoutPaths
from scout.scheduler import run_due

DAY = date(2026, 11, 2)
HERE = Path(__file__).resolve().parent.parent / "scout"
HEADINGS = ("# Scout brief", "## Macro events", "## Inflation regime and VIX", "## Jobs and inflation results",
            "## Overnight catalysts",
            "## Open halts", "## Earnings due windows", "## Jason's levels", "## Paper Gains Discord", "## Source health",
            "## Polling pace", "## Claude API",
            "## Verification sample")


def _radar(tmp_path: Path) -> Path:
    finds = tmp_path / "Radar" / "state" / "handoff"
    finds.mkdir(parents=True)
    (finds / "2026-11-02.jsonl").write_text(
        json.dumps({"format": 1, "candidate_id": "c1", "symbol": "ABC", "found_at": "2026-11-02T13:40:00Z", "price": 3.2}) + "\n"
        + json.dumps({"format": 1, "candidate_id": "c2", "symbol": "BIG", "found_at": "2026-11-02T13:41:00Z", "price": 250.0})
        + "\n", encoding="utf-8")
    return tmp_path / "Radar"


def _state(scout_dir: Path) -> None:
    write_records(scout_dir, "facts", [
        fact("ABC", "ABC wins **[contract](http://evil)** <img src=x> # now", "2026-11-02T12:00:00Z", fid="fa"),
        fact("OLD", "Old news before the close", "2026-10-30T19:00:00Z", fid="fo"),
        fact("LATE", "Seen after the brief", "2026-11-02T15:00:00Z", fid="fl"),
        fact("BIG", "BIG Corp wins order", "2026-11-02T12:30:00Z", fid="fb"),
        fact("HLT", "halt", "2026-11-02T13:30:00Z", fid="h1", kind="halt", cls=None, halt_time="2026-11-02T13:30:00Z",
             halt_code="T1"),
        fact("RSM", "halt", "2026-11-02T13:00:00Z", fid="h2", kind="halt", cls=None, halt_time="2026-11-02T13:00:00Z",
             halt_code="LUDP"),
        fact("RSM", "halt", "2026-11-02T13:06:00Z", fid="h3", kind="halt", cls=None, halt_time="2026-11-02T13:00:00Z",
             halt_code="LUDP", resumed_at="2026-11-02T13:05:00Z"),
    ])
    (scout_dir / "state" / "macro" / "2026-11-02.json").write_text(json.dumps({
        "format": 1, "at": "2026-11-02T13:20:00Z", "inflation_regime": "cooling", "regime_status": "ok",
        "vix": {"date": "2026-10-30", "close": 17.4}, "sources": {},
        "events": [{"kind": "CPI", "event_at": "2026-11-03T13:30:00Z", "prior": 2.9, "street": None,
                    "nowflation_forecast": 2.8, "sources": ["nowflation", "bls"], "sources_agree": True}]}),
        encoding="utf-8")
    write_records(scout_dir, "due", [{"ticker": "ABC", "cik": 1, "computed_at": "2026-11-02T13:00:00Z", "status": "ok",
                                      "due_start": "2026-11-10", "due_end": "2026-12-20", "overdue": False,
                                      "nt_recent": True}])
    write_records(scout_dir, "jason", [{"first_seen_at": "2026-10-28T00:00:00Z", "today_et": "2026-10-27",
                                        "files": ["2026-10-27.tuesday.json"], "warnings": [], "setups": [
        {"symbol": "SPY", "direction": "bullish", "attribution": "jason", "source_kind": "tuesday",
         "session_date": "2026-10-27", "expires": "2026-11-03", "horizon": None,
         "levels": [{"kind": "support", "readings": ["580.5"], "ambiguous": False, "note": None}]},
        {"symbol": "TSLA", "direction": "bearish", "attribution": "jason", "source_kind": "tuesday",
         "session_date": "2026-10-27", "expires": "2026-10-30", "horizon": None, "levels": []}]}])
    (scout_dir / "state" / "scout.heartbeat").write_text(json.dumps({
        "format": 1, "at": "2026-11-02T13:59:50Z", "version": "0.1.0", "stopped": False,
        "sources": {"prnewswire": {"enabled": True, "status": "ok", "last_ok": "2026-11-02T13:59:00Z",
                                   "last_error": None, "gap_open": False},
                    "testwire": {"enabled": True, "status": "refused", "last_ok": None, "last_error": "403",
                                      "gap_open": True}},
        "llm": {}, "levels": {}, "calendar_covers_next_45d": True}), encoding="utf-8")
    (scout_dir / "state" / "verify" / "samples.jsonl").write_text(json.dumps({
        "format": 1, "week": "2026-W45", "seed": "s", "picked_at": "2026-11-02T01:00:00Z", "symbols": ["ABC", "XYZ"]})
        + "\n", encoding="utf-8")


def test_empty_state_skeleton(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock, write_audit) -> None:  # noqa: ANN001
    ctx = make_ctx(cfg, paths, clock)
    with write_audit.watching(paths.reports):
        path = write_brief(ctx, DAY, clock.now())
    assert path == brief_path(ctx, DAY) == paths.reports / "scout-2026-11-02.md"
    text = path.read_text(encoding="utf-8")
    for heading in HEADINGS:
        assert heading in text
    assert "[Nowflation](https://nowflation.com)" in text and "No macro data yet" in text
    assert "09:00 ET / 00:00 AEST" in text
    assert "**No current levels file.**" in text and "LLM off" in text and "No heartbeat yet" in text
    assert "radar_dir is empty" in text and "No sample yet" in text and "Claude's paragraph" not in text
    assert "No BLS results yet." in text
    assert "No pace worked out yet" in text  # 0.2.4: no plan saved yet, every source at its fixed spacing
    assert len(SECTIONS) == 14


def test_full_brief(scout_dir: Path, clock: ManualClock, tmp_path: Path) -> None:
    cfg = make_config(scout_dir, radar_dir=str(_radar(tmp_path)))
    paths = ScoutPaths.from_config(cfg)
    _state(scout_dir)
    text = build_brief(make_ctx(cfg, paths, clock), DAY, clock.now())
    assert "Tue 03 Nov 08:30 ET / 23:30 AEST: CPI; prior 2.9; nowflation_forecast 2.8; from nowflation, bls" in text
    assert "Inflation regime: cooling (ok)" in text and "VIX close: 17.4 on 2026-10-30" in text
    assert "**ABC**" in text and "&lt;img src=x&gt;" in text and "\\*\\*\\[contract\\]\\(http://evil\\)" in text
    assert "<img" not in text and "OLD" not in text and "LATE" not in text
    assert "**BIG**" not in text  # a Radar find prices BIG above scan_max_price
    assert "**HLT** halted" in text and "RSM" not in text
    assert "ABC: ok, due 2026-11-10 to 2026-12-20 (late-filing notice)" in text and "BIG: no due-window record" in text
    assert "Market frame:" in text and "**SPY** bullish" in text and "support 580.5" in text and "TSLA" not in text
    assert "testwire: refused" in text and "coverage gap open" in text
    assert "python -m scout check`: ABC, XYZ" in text


def test_past_date_uses_as_of(scout_dir: Path, clock: ManualClock, cfg: ScoutConfig, paths: ScoutPaths) -> None:
    write_records(scout_dir, "facts", [fact("EARLY", "early news", "2026-10-30T21:00:00Z", fid="e1"),
                                       fact("NEXT", "next day news", "2026-11-02T12:00:00Z", fid="n1")])
    clock.set(clock.now().replace(day=3))
    text = build_brief(make_ctx(cfg, paths, clock), DAY, clock.now())
    assert "**EARLY**" in text and "**NEXT**" in text
    text_fri = build_brief(make_ctx(cfg, paths, clock), date(2026, 10, 30), clock.now())
    assert "Friday 30 October 2026" in text_fri and "NEXT" not in text_fri and "EARLY" not in text_fri


def test_llm_cap_reached_and_j4(scout_dir: Path, clock: ManualClock) -> None:
    cfg = make_config(scout_dir, llm_enabled=True)
    paths = ScoutPaths.from_config(cfg)
    write_records(scout_dir, "facts", [fact("ABC", "ABC wins contract", "2026-11-02T12:00:00Z", fid="fa")])
    tr = FakeAnthropic(message({"paragraph": "Calm open.", "watch_list": [
        {"ticker": "ABC", "reason": "contract", "evidence_ids": ["C1"]}]}, model="claude-sonnet-5-5"))
    ctx = make_ctx(cfg, paths, clock, tr)
    text = write_brief(ctx, DAY, clock.now()).read_text(encoding="utf-8")
    assert "Claude's paragraph" in text and "Calm open." in text and "- ABC: contract (items C1)" in text
    assert "Spend today $" in text and "LLM cap reached" not in text
    write_records(scout_dir, "llm", [{"job": "J1", "at": "2026-11-02T13:00:00Z", "cost_usd": 1.0, "validation": "ok",
                                      "usage": {}}])
    tr2 = FakeAnthropic()
    text = write_brief(make_ctx(cfg, paths, clock, tr2), DAY, clock.now()).read_text(encoding="utf-8")
    assert "**LLM cap reached:**" in text and "Claude's paragraph" not in text and tr2.calls == []


def test_j4_items_are_public(scout_dir: Path, clock: ManualClock, tmp_path: Path) -> None:
    cfg = make_config(scout_dir, radar_dir=str(_radar(tmp_path)))
    paths = ScoutPaths.from_config(cfg)
    _state(scout_dir)
    from scout.brief import collect  # noqa: PLC0415

    items = j4_items(collect(make_ctx(cfg, paths, clock), DAY, clock.now()))
    assert [i["id"] for i in items] == ["C1", "H1", "M1"]
    assert str(scout_dir) not in json.dumps(items) and "price" not in json.dumps(items)


def test_cli_brief(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock, capsys: pytest.CaptureFixture) -> None:
    ctx = make_ctx(cfg, paths, clock)
    assert cli_brief(ctx, argparse.Namespace(date="2026-13-40")) == 2
    assert cli_brief(ctx, argparse.Namespace(date="2027-01-05")) == 2
    assert cli_brief(ctx, argparse.Namespace(date=None)) == 0
    assert "scout-2026-11-02.md" in capsys.readouterr().out


def test_brief_reads_only_through_reader_and_store() -> None:
    for name in ("brief.py", "brief_sections.py"):
        tree = ast.parse((HERE / name).read_text(encoding="utf-8"))
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        attrs = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert "open" not in calls and not attrs & {"read_text", "read_bytes", "open", "write_text"}, name


def test_jobs_brief(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock, scout_dir: Path) -> None:
    ctx = make_ctx(cfg, paths, clock)
    jobs = build_jobs(ctx)
    assert [j.name for j in jobs] == ["brief", "verify", "ledger_status"]
    ctx.live_beat_at = clock.now()
    ran = run_due(jobs, clock.now(), logging.getLogger("test"))
    assert ran == ["brief", "ledger_status"]  # 09:00 ET: the brief (08:20) is due, verify (20:30) is not
    assert (paths.reports / "scout-2026-11-02.md").exists()
    assert ctx.board.llm == {"enabled": False, "spent_today_usd": 0.0, "cap_reached": False}
    clock.set(clock.now().replace(day=3, hour=1, minute=31))  # 20:31 ET Monday
    write_records(scout_dir, "answers", [{"symbol": "ABC", "as_of": "2026-11-03T00:30:00Z",
                                          "written_at": "2026-11-03T00:30:00Z", "origin": "pilot",
                                          "fresh_catalyst": "true", "event_before_next_session": "unknown",
                                          "macro_event_next_session": "false", "news_headline": "true",
                                          "halt_open": "false", "reasons": {}, "evidence": {}, "stale": False}])
    assert "verify" in run_due(jobs, clock.now(), logging.getLogger("test"))
    assert (paths.reports / "scout-verify-2026-11-02.md").exists()
    assert (paths.verify / "samples.jsonl").exists()  # Monday is the sample weekday


def test_the_nowflation_forecast_lean_is_shown(scout_dir: Path, clock: ManualClock, cfg: ScoutConfig,
                                               paths: ScoutPaths) -> None:
    """Regression: forecast_lean was built and stored in macro/history.jsonl but no brief section
    showed it, so the owner never saw the lean or its 'informational, untested' caveat (SS 7.3)."""
    write_records(scout_dir, "macro", [{
        "source": "nowflation", "first_seen_at": "2026-11-02T13:20:00Z", "fetched_at": "2026-11-02T13:20:00Z",
        "status": "ok", "reasons": [], "source_generated_at": "2026-11-02T11:05:00Z", "sha256": None,
        "stale_after": "2026-11-03T14:15:00Z", "inflation_regime": "hot", "vix": None, "realtime": None,
        "attribution": "Nowflation https://nowflation.com", "events": [
            {"kind": "cpi", "event_at": "2026-11-03T13:30:00Z", "prior": 0.3, "street": None,
             "nowflation_forecast": 0.2}],
        "forecast_lean": [{"kind": "cpi", "event_at": "2026-11-03T13:30:00Z", "vs_prior": -0.1, "vs_street": None,
                           "graded_prints": 2, "note": "informational, untested"}]}])
    text = build_brief(make_ctx(cfg, paths, clock), DAY, clock.now())
    assert "Nowflation forecast lean (informational, untested):" in text
    assert "cpi forecast vs prior -0.1, vs street n/a; graded past prints 2" in text


def test_the_lean_shows_cleveland_and_the_receipt_status_when_given(scout_dir: Path, clock: ManualClock,
                                                                     cfg: ScoutConfig, paths: ScoutPaths) -> None:
    write_records(scout_dir, "macro", [{
        "source": "nowflation", "first_seen_at": "2026-11-02T13:20:00Z", "fetched_at": "2026-11-02T13:20:00Z",
        "status": "ok", "reasons": [], "source_generated_at": "2026-11-02T11:05:00Z", "sha256": None,
        "stale_after": "2026-11-03T14:15:00Z", "inflation_regime": "steady", "vix": None, "realtime": None,
        "attribution": "Nowflation https://nowflation.com", "events": [],
        "forecast_lean": [{"kind": "cpi", "event_at": "2026-11-03T13:30:00Z", "vs_prior": -0.19, "vs_street": None,
                           "vs_cleveland": 0.13, "receipt_status": "provisional", "graded_prints": 3,
                           "note": "informational, untested"}]}])
    text = build_brief(make_ctx(cfg, paths, clock), DAY, clock.now())
    assert ("cpi forecast vs prior -0.19, vs street n/a, vs Cleveland Fed +0.13 (receipt provisional); "
            "graded past prints 3") in text


def test_a_lean_without_a_cleveland_value_leaves_cleveland_out(scout_dir: Path, clock: ManualClock,
                                                                cfg: ScoutConfig, paths: ScoutPaths) -> None:
    """Review: forecast_lean always writes `vs_cleveland` (None when neither the calendar nor the
    receipt has a Cleveland value), so a presence check printed 'vs Cleveland Fed n/a' every time."""
    write_records(scout_dir, "macro", [{
        "source": "nowflation", "first_seen_at": "2026-11-02T13:20:00Z", "fetched_at": "2026-11-02T13:20:00Z",
        "status": "ok", "reasons": [], "source_generated_at": "2026-11-02T11:05:00Z", "sha256": None,
        "stale_after": "2026-11-03T14:15:00Z", "inflation_regime": "steady", "vix": None, "realtime": None,
        "attribution": "Nowflation https://nowflation.com", "events": [],
        "forecast_lean": [{"kind": "cpi", "event_at": "2026-11-03T13:30:00Z", "vs_prior": -0.19, "vs_street": None,
                           "vs_cleveland": None, "receipt_status": "provisional", "graded_prints": 3,
                           "note": "informational, untested"}]}])
    text = build_brief(make_ctx(cfg, paths, clock), DAY, clock.now())
    assert "cpi forecast vs prior -0.19, vs street n/a (receipt provisional); graded past prints 3" in text
    assert "Cleveland" not in text


def test_a_restart_after_the_brief_time_keeps_todays_brief(cfg: ScoutConfig, paths: ScoutPaths,
                                                           clock: ManualClock) -> None:
    """Regression (third review): the brief job was a plain DailyJob, so a restart after 08:20 ET
    rewrote today's good brief on the first tick, from the "starting" heartbeat."""
    import os  # noqa: PLC0415

    from scout.app import run  # noqa: PLC0415
    from scout.clock import et_datetime  # noqa: PLC0415

    ctx = make_ctx(cfg, paths, clock)
    paths.ensure()
    target = paths.reports / "scout-2026-11-02.md"
    target.write_text("the 08:20 brief\n", encoding="utf-8")
    stamp = et_datetime(DAY, cfg.brief_time_et).timestamp()
    os.utime(target, (stamp, stamp))
    assert run(ctx, max_ticks=3, jobs=build_jobs(ctx)) == 0
    assert target.read_text(encoding="utf-8") == "the 08:20 brief\n"


def test_a_brief_written_by_hand_before_the_brief_time_is_replaced(cfg: ScoutConfig, paths: ScoutPaths,
                                                                   clock: ManualClock) -> None:
    import os  # noqa: PLC0415

    from scout.app import run  # noqa: PLC0415
    from scout.clock import et_datetime  # noqa: PLC0415

    ctx = make_ctx(cfg, paths, clock)
    paths.ensure()
    target = paths.reports / "scout-2026-11-02.md"
    target.write_text("a 06:00 brief\n", encoding="utf-8")
    stamp = et_datetime(DAY, cfg.brief_time_et).timestamp() - 2 * 3600
    os.utime(target, (stamp, stamp))
    assert run(ctx, max_ticks=3, jobs=build_jobs(ctx)) == 0
    assert target.read_text(encoding="utf-8").startswith("# Scout brief")


def test_a_start_after_the_brief_time_writes_the_brief_after_a_live_beat(cfg: ScoutConfig, paths: ScoutPaths,
                                                                         clock: ManualClock) -> None:
    """Regression (third review): the first-tick brief read the "starting" heartbeat and said
    "STALE or stopped" with no source lines."""
    from scout.app import run  # noqa: PLC0415

    ctx = make_ctx(cfg, paths, clock)
    assert run(ctx, max_ticks=3, jobs=build_jobs(ctx)) == 0
    text = (paths.reports / "scout-2026-11-02.md").read_text(encoding="utf-8")
    assert "STALE" not in text


def test_overnight_catalysts_are_newest_first_and_bank_notes_are_folded(scout_dir: Path, cfg: ScoutConfig,
                                                                          paths: ScoutPaths,
                                                                          clock: ManualClock) -> None:
    """Regression (third review): the list was the oldest 40 since the close, so a flood of filings
    between 16:00 and 16:30 ET hid every overnight and pre-market release; a bank's 424B2 notes
    filled it; and a first poll's backlog from the previous day came in with one first_seen_at."""
    from scout.brief_sections import sec_catalysts  # noqa: PLC0415

    flood = [fact("MS", f"424B2 - MORGAN STANLEY note {n}", f"2026-10-30T21:{n % 60:02d}:00Z", fid=f"ms{n:03d}",
                  kind="filing", cls="offering") for n in range(60)]
    for f in flood:
        f["cik"] = 895421
    early = [fact(f"E{n:02d}", f"Early {n} wins contract", f"2026-10-30T20:{n % 60:02d}:30Z", fid=f"e{n:03d}")
             for n in range(45)]
    backlog = fact("OLDP", "Published yesterday, seen on the first poll", "2026-11-02T08:00:00Z", fid="bk",
                   published_at="2026-10-29T15:00:00Z")
    premarket = fact("PRE", "Pre-market contract award", "2026-11-02T12:00:00Z", fid="pm")
    write_records(scout_dir, "facts", [*flood, *early, backlog, premarket])
    inp = collect(make_ctx(cfg, paths, clock), DAY, clock.now())
    lines = [x for x in sec_catalysts(inp) if x.startswith("- ")]
    assert "PRE" in lines[0]  # the newest item comes first
    assert not any("OLDP" in x for x in lines)  # published before the prior close
    assert sum("MORGAN STANLEY" in x for x in lines) == 1
    assert "(+59 more offering filings by this issuer)" in "\n".join(lines)
