"""Fixes and keys added at integration (DESIGN 15): the brief's macro view as of its own time, the
config keys that replaced unit constants, and the loop's `until` stop."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, time, timedelta
from pathlib import Path

from conftest import et_at, make_config
from helpers_wire import fixture, wire_config

import scout.levels_score as ls
from scout import FORMAT
from scout.app import AppContext, run
from scout.brief import build_brief
from scout.clock import ManualClock, iso
from scout.config import ScoutConfig
from scout.heartbeat import SourceStatusBoard
from scout.jobs_brief import build_jobs as brief_jobs
from scout.macro import daily_as_of
from scout.paths import ScoutPaths
from scout.scheduler import EveryJob
from scout.store import append_jsonl

DAY = "2026-11-02"


def _ctx(cfg: ScoutConfig, clock: ManualClock) -> AppContext:
    return AppContext(cfg=cfg, paths=ScoutPaths.from_config(cfg), clock=clock, board=SourceStatusBoard(cfg),
                      log=logging.getLogger("test.integration"))


def _history(paths: ScoutPaths, source: str, fetched: str, **over: object) -> None:
    rec: dict[str, object] = {
        "format": FORMAT, "source": source, "status": "ok", "fetched_at": fetched, "first_seen_at": fetched,
        "events": [], "inflation_regime": None, "realtime": None, "reasons": [], "vix": None, "attribution": None,
        "forecast_lean": [], "sha256": None, "source_generated_at": None,
        "stale_after": iso(et_at(DAY, "23:00")),
    }
    rec.update(over)
    append_jsonl(paths, paths.macro / "history.jsonl", rec)


# --------------------------------------------------------------------------- the brief's macro view


def test_macro_view_is_rebuilt_as_of(paths: ScoutPaths, cfg: ScoutConfig) -> None:
    nfp = {"kind": "nfp", "event_at": "2026-11-06T13:30:00Z", "prior": 140, "street": None,
           "nowflation_forecast": None, "cleveland": None, "kalshi": None, "extra": {"title": "NFP"}}
    _history(paths, "cboe_vix", iso(et_at(DAY, "03:55")), vix={"close": 17.1, "date": "2026-10-30"})
    _history(paths, "nowflation", iso(et_at(DAY, "08:20")), inflation_regime="hot", events=[nfp])
    assert daily_as_of(paths, cfg, et_at(DAY, "03:00")) is None
    early = daily_as_of(paths, cfg, et_at(DAY, "08:00"))
    assert early is not None and early["vix"]["close"] == 17.1
    assert early["inflation_regime"] == "unknown" and early["events"] == []
    late = daily_as_of(paths, cfg, et_at(DAY, "08:21"))
    assert late is not None and late["inflation_regime"] == "hot"
    assert [e["kind"] for e in late["events"]] == ["nfp"]


def test_the_brief_sees_a_read_made_in_its_own_tick(cfg: ScoutConfig) -> None:
    """At 08:20 the nowflation read and the brief run in the same tick; the brief job writes as of
    the moment it runs, so the read is in it (it once said "No macro data yet")."""
    clock = ManualClock(et_at(DAY, "08:20"))
    ctx = _ctx(cfg, clock)
    tick = clock.now()
    clock.advance(3)  # the nowflation read takes three seconds
    _history(ctx.paths, "nowflation", iso(clock.now()), inflation_regime="hot")
    brief = next(j for j in brief_jobs(ctx) if j.name == "brief")
    brief.run(tick)
    text = (ctx.paths.reports / f"scout-{DAY}.md").read_text(encoding="utf-8")
    assert "Inflation regime: hot" in text
    assert "Inflation regime: hot" not in build_brief(ctx, date(2026, 11, 2), tick)  # as of the tick itself


# --------------------------------------------------------------------------- keys that replaced constants


def test_verify_runs_at_verify_time_et(scout_dir: Path) -> None:
    cfg = make_config(scout_dir, verify_time_et="21:10")
    job = next(j for j in brief_jobs(_ctx(cfg, ManualClock(et_at(DAY, "09:00")))) if j.name == "verify")
    assert job.at_et == time(21, 10)
    assert not job.due(et_at(DAY, "21:09")) and job.due(et_at(DAY, "21:10"))


def test_ticker_map_retry_follows_the_config(scout_dir: Path, paths: ScoutPaths, clock: ManualClock) -> None:
    from test_edgar_submissions import map_client, tickers_url  # noqa: PLC0415

    cfg = wire_config(scout_dir, ticker_map_retry_s=120)
    good = fixture("company_tickers_exchange_excerpt.json")
    client, fetcher, _health, _board = map_client(cfg, paths, clock, {})
    assert client.ensure(clock.now()) is True
    assert client.ensure(clock.now() + timedelta(seconds=119)) is False
    fetcher.results[tickers_url(cfg)] = good
    assert client.ensure(clock.now() + timedelta(seconds=120)) is True


def test_long_horizon_sessions_follow_the_config(cfg: ScoutConfig) -> None:
    setup = {"session_date": "2026-09-15", "expires": "2026-09-22", "horizon": "a few months"}
    assert ls.window_end(setup, cfg.levels_long_horizon_sessions) == date(2026, 9, 29)
    assert ls.window_end(setup, 15) == date(2026, 10, 6)


def test_reader_params_carry_the_market_frame(scout_dir: Path) -> None:
    cfg = make_config(scout_dir, jason_market_symbols=["SPY", "IWM"])
    assert cfg.reader_params()["jason_market_symbols"] == ["SPY", "IWM"]


# --------------------------------------------------------------------------- the loop


def test_run_until_stops_at_the_clock_time(cfg: ScoutConfig) -> None:
    clock = ManualClock(et_at(DAY, "10:00"))
    ctx = _ctx(replace(cfg, loop_interval_s=10.0), clock)
    seen: list[str] = []
    job = EveryJob("tick", lambda now: seen.append(iso(now)), 30)
    assert run(ctx, jobs=[job], until=et_at(DAY, "10:02")) == 0
    assert len(seen) == 4  # 10:00:00, :30, 10:01:00, :30
    assert clock.now() >= et_at(DAY, "10:02")


# --------------------------------------------------------------------------- the request shape vs the pinned SDK


def test_request_params_match_the_pinned_sdk(cfg: ScoutConfig) -> None:
    """Offline: every key Scout sends is a parameter of the installed SDK's `messages.create`, and
    the output_config and web tool types exist in it (the pin is anthropic==2.3.0; no call is made)."""
    import inspect  # noqa: PLC0415
    import typing  # noqa: PLC0415

    import pytest  # noqa: PLC0415

    anthropic = pytest.importorskip("anthropic")
    from anthropic.resources.messages import Messages  # noqa: PLC0415

    from helpers_fake_anthropic import fact  # noqa: PLC0415
    from scout.llm_prompts import build_j1, build_j3, build_j4  # noqa: PLC0415

    accepted = set(inspect.signature(Messages.create).parameters)
    output_keys = set(typing.get_type_hints(anthropic.types.OutputConfigParam))
    f = fact("ACME", "Acme wins a contract", iso(et_at(DAY, "09:00")))
    requests = [build_j1([f], cfg), build_j3("ACME", None, [f], cfg),
                build_j4([{"id": "C1", "text": "Acme wins a contract", "ticker": "ACME"}], cfg)]
    sdk_tool_types = {n for n in dir(anthropic.types) if n.startswith(("WebSearchTool2", "WebFetchTool2"))}
    for req in requests:
        params = req.params()
        assert set(params) <= accepted, set(params) - accepted
        assert set(params["output_config"]) <= output_keys
        for tool in params.get("tools", []):
            stamp = tool["type"].rsplit("_", 1)[1]
            kind = "WebSearchTool" if tool["type"].startswith("web_search") else "WebFetchTool"
            assert f"{kind}{stamp}Param" in sdk_tool_types, tool["type"]
