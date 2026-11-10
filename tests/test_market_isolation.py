"""The market feed changes nothing Scout tells the other bots, and never leaks the key (0.3.2; build
plan amendment B; SCOUT_SPEC 2.5, 2.7, 2.8, 19.5). No network, no real key."""

from __future__ import annotations

import logging
import socket
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_EMAIL
from helpers_fakes import FakeResponse
from helpers_market import FAKE_ID, FAKE_SECRET, FakeAlpaca, live_answer, ok
from test_market_feed import WED, et

from scout.app import AppContext, load_jobs, run
from scout.clock import ManualClock
from scout.config import config_from_dict
from scout.heartbeat import SourceStatusBoard
from scout.http import HttpFetcher
from scout.logs import close_logging, setup_logging
from scout.market_feed import DELAYED, LIVE, MarketPoller
from scout.paths import ScoutPaths
from scout.raw import RawStore
from scout.reads_market import market_lines
from scout.secrets import alpaca_key

# the same settings in every case, so params (and its config hash) may be compared byte for byte;
# no spacing for the data host, so the fake requests never move the test clock
SETTINGS = {"contact_email": TEST_EMAIL, "reads_enabled": True, "host_min_spacing_s": {"data.alpaca.markets": 0}}


def _set_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APCA_API_KEY_ID", FAKE_ID)
    monkeypatch.setenv("APCA_API_SECRET_KEY", FAKE_SECRET)


def _run(root: Path, mode: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root.mkdir(parents=True)
    cfg = config_from_dict(SETTINGS, root)
    paths = ScoutPaths.from_config(cfg)
    paths.ensure()
    clock = ManualClock(et(WED, "10:02"))
    ctx = AppContext(cfg=cfg, paths=paths, clock=clock, board=SourceStatusBoard(cfg), log=logging.getLogger("t.iso"))
    if mode == "off":
        monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
        monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    else:
        _set_key(monkeypatch)
    failing = FakeResponse(500, {}, b"")
    fake = FakeAlpaca(clock, **({LIVE: failing, DELAYED: failing, "closes": failing} if mode == "failing" else {}))
    ctx._services["fetcher"] = HttpFetcher(cfg, clock, transport=fake)
    jobs = load_jobs(ctx, modules=("scout.jobs_market",))
    assert len(jobs) == (0 if mode == "off" else 1)
    assert run(ctx, max_ticks=200, jobs=jobs, pulse=False) == 0
    state = paths.state
    return {
        "heartbeat": paths.heartbeat_file.read_bytes(),
        "beats": {p.name: p.read_bytes() for p in sorted(paths.beats.glob("*"))},
        "health": {p.name: p.read_bytes() for p in sorted(paths.health.rglob("*")) if p.is_file()},
        "params": paths.params_file.read_bytes(),
        "board": dict(ctx.board.sources),
        "market": market_lines(paths, WED),
        "calls": len(fake.calls),
        "state_files": sorted(str(p.relative_to(state)) for p in state.rglob("*") if p.is_file()),
    }


def test_heartbeat_board_health_and_params_identical_recording_failing_and_off(tmp_path: Path,
                                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    got = {mode: _run(tmp_path / mode, mode, monkeypatch) for mode in ("recording", "failing", "off")}
    assert [r["kind"] for r in got["recording"]["market"]] and {r["kind"] for r in got["recording"]["market"]} == {
        "market"}
    assert {r["kind"] for r in got["failing"]["market"]} == {"miss"} and got["off"]["market"] == []
    assert got["recording"]["calls"] > 3 and got["off"]["calls"] == 0
    for key in ("heartbeat", "beats", "health", "params", "board"):
        assert got["recording"][key] == got["failing"][key] == got["off"][key], key
    for mode in got:
        text = got[mode]["heartbeat"] + got[mode]["params"] + b"".join(got[mode]["beats"].values())
        assert b"alpaca" not in text.lower() and b"market_feed" not in text
        assert "alpaca_market" not in got[mode]["board"]
    # nothing but the market file is new (this test's fetcher keeps no host-state file)
    extra = set(got["recording"]["state_files"]) - set(got["off"]["state_files"])
    assert extra == {str(Path("reads") / "market" / "2026-11-11.jsonl")}


def test_not_paced_not_gate6_not_a_source(scout_dir: Path) -> None:
    cfg = config_from_dict(SETTINGS, scout_dir)
    assert "alpaca_market" not in cfg.sources and not cfg.enabled_sources("market")
    assert "alpaca_market" not in str(cfg.reader_params())
    assert cfg.pace.paced("alpaca_market") is False


def test_answers_never_read_the_market_file(scout_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_reads_isolation import _answers, _hook, _opened, _watching  # noqa: F401, PLC0415

    cfg = config_from_dict(SETTINGS, scout_dir)
    paths = ScoutPaths.from_config(cfg)
    clock = ManualClock(et(WED, "10:02"))
    p = MarketPoller(cfg, paths, HttpFetcher(cfg, clock, transport=FakeAlpaca(clock)), _key(monkeypatch), clock)
    before = _answers(scout_dir)
    p.tick(clock.now())
    assert market_lines(paths, WED)
    _opened.clear()
    _watching[0] = True
    try:
        after = _answers(scout_dir)
    finally:
        _watching[0] = False
    assert after == before
    assert not [x for x in _opened if "market" in Path(x).parts and "reads" in Path(x).parts]


def _key(monkeypatch: pytest.MonkeyPatch) -> Any:
    _set_key(monkeypatch)
    key = alpaca_key()
    assert key is not None
    return key


# --------------------------------------------------------------------------- the key never leaks


def _modes(clock: ManualClock) -> list[dict[str, Any]]:
    bad = FakeResponse(403, {}, b'{"code":40310000,"message":"request is not authorized"}')
    return [
        {}, {LIVE: bad, DELAYED: bad, "closes": bad}, {LIVE: FakeResponse(401, {}, b"")},
        {LIVE: FakeResponse(429, {}, b"")}, {LIVE: FakeResponse(500, {}, b"")}, {LIVE: socket.timeout("x")},
        {LIVE: OSError("reset")}, {LIVE: ok({"SPY": {"latestTrade": {"t": "bad", "p": 1}}})},
        {LIVE: FakeResponse(200, {}, b"{bad")}, {LIVE: live_answer(clock.now(), skip=("QQQ",))},
        {LIVE: FakeResponse(301, {"location": "https://paper-api.alpaca.markets/v2/orders"}, b"")},
        {DELAYED: ok({"bars": {}, "next_page_token": "x"})},
    ]


def test_no_secret_in_logs_records_raw_errors_or_reprs(scout_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                       caplog: pytest.LogCaptureFixture) -> None:
    cfg = config_from_dict({**SETTINGS, "log_level": "DEBUG"}, scout_dir)
    paths = ScoutPaths.from_config(cfg)
    key = _key(monkeypatch)
    clock = ManualClock(et(WED, "10:02"))
    log = setup_logging(paths, "DEBUG", WED, cfg)
    seen: list[Any] = []
    texts: list[str] = [repr(key), str(key)]
    try:
        with caplog.at_level(logging.DEBUG):
            for routes in _modes(clock):
                fake = FakeAlpaca(clock, **routes)
                fetcher = HttpFetcher(cfg, clock, transport=fake, log=log.getChild("http"))
                real_fetch = fetcher.fetch

                def capture(*a: Any, _f: Any = real_fetch, **k: Any) -> Any:
                    got = _f(*a, **k)
                    seen.append(got)
                    return got

                fetcher.fetch = capture  # type: ignore[method-assign]
                p = MarketPoller(cfg, paths, fetcher, key, clock, raw=RawStore(paths), log=log.getChild("market"))
                for _ in range(3):
                    p.tick(clock.now())
                    out = p.poll(clock.now())
                    texts += [repr(out), repr(p), repr(fetcher), str(out.legs)]
                    clock.advance(400)
                clock.set(et(WED, "10:02") + timedelta(seconds=1))
    finally:
        close_logging()
    texts += [caplog.text, *(repr(r) for r in seen), *(str(r.error) for r in seen), *(str(r.url) for r in seen),
              *(str(r.headers) for r in seen)]
    for value in (FAKE_ID, FAKE_SECRET):
        assert not [t for t in texts if value in t]
        leaks = [p for p in paths.root.rglob("*") if p.is_file() and value.encode() in p.read_bytes()]
        assert leaks == []
    assert any(p.suffix == ".log" for p in paths.logs.iterdir())
    assert seen and all(r.body is None or FAKE_ID.encode() not in r.body for r in seen)
    logging.getLogger("scout").handlers.clear()


def test_no_key_is_off_with_one_log_line(scout_dir: Path, caplog: pytest.LogCaptureFixture,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    from scout.jobs_market import build_jobs  # noqa: PLC0415

    def ctx_for(**over: Any) -> AppContext:
        cfg = config_from_dict({**SETTINGS, **over}, scout_dir)
        return AppContext(cfg=cfg, paths=ScoutPaths.from_config(cfg), clock=ManualClock(et(WED, "10:02")),
                          board=SourceStatusBoard(cfg), log=logging.getLogger("t.jobs"))

    with caplog.at_level(logging.INFO, logger="t.jobs"):
        assert build_jobs(ctx_for()) == []
    msgs = [r.getMessage() for r in caplog.records if r.name == "t.jobs"]
    assert len(msgs) == 1 and "APCA_API_KEY_ID" in msgs[0] and "ALPACA_SECRET_KEY" in msgs[0]
    monkeypatch.setenv("ALPACA_API_KEY", FAKE_ID)  # only one half: still off
    assert build_jobs(ctx_for()) == []
    monkeypatch.setenv("ALPACA_API_SECRET", FAKE_SECRET)
    caplog.clear()
    jobs = build_jobs(ctx_for())
    assert [j.name for j in jobs] == ["market"] and jobs[0].interval_s == 60.0
    assert build_jobs(ctx_for(reads_enabled=False)) == [] and build_jobs(ctx_for(reads_market_enabled=False)) == []
    assert build_jobs(ctx_for(contact_email="")) == []
    assert FAKE_ID not in caplog.text and FAKE_SECRET not in caplog.text


def test_the_previous_closes_are_held_per_day_only(scout_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = config_from_dict(SETTINGS, scout_dir)
    paths = ScoutPaths.from_config(cfg)
    clock = ManualClock(et(WED, "19:59"))
    fake = FakeAlpaca(clock)
    p = MarketPoller(cfg, paths, HttpFetcher(cfg, clock, transport=fake), _key(monkeypatch), clock)
    p.tick(clock.now())
    assert [r.get("prev_day") for r in market_lines(paths, WED) if r["kind"] == "market"] == ["2026-11-10"]
    clock.set(et(date(2026, 11, 12), "04:00"))
    fake.prev_day = WED
    p.tick(clock.now())  # at 04:00 every price is from before the day's start: only "stale", but the closes roll
    assert fake.legs().count("closes") == 2
    assert [(r["kind"], r["reason"]) for r in market_lines(paths, date(2026, 11, 12))] == [("miss", "stale")]
    clock.set(et(date(2026, 11, 12), "09:31"))
    p.tick(clock.now())
    recs = market_lines(paths, date(2026, 11, 12))
    assert fake.legs().count("closes") == 2  # held for the new day: not asked again
    assert [(r.get("prev_day"), r.get("trading_day")) for r in recs if r["kind"] == "market"] == [
        ("2026-11-11", "2026-11-12")]
