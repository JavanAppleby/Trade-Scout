"""The spend ledger and caps (SCOUT_SPEC 8.6, 14 "the ledger refuses the call that would cross a cap;
at the cap Layer 1 keeps running")."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest

from conftest import make_config
from helpers_fake_anthropic import FakeAnthropic, fact, j1_reply, label
from helpers_fakes import write_records
from scout.clock import ManualClock
from scout.config import ScoutConfig
from scout.llm_j1 import J1Job
from scout.llm_ledger import CHARS_PER_TOKEN, Ledger
from scout.llm_prompts import build_j1, build_j3
from scout.paths import ScoutPaths
from scout.scheduler import EveryJob, run_due

T = "2026-11-02T13:00:00Z"


def _call(at: str, *, job: str = "J1", cost: float = 0.0, validation: str = "ok", searches: int = 0,
          reason: str | None = None) -> dict:
    return {"job": job, "at": at, "model_id": "claude-haiku-5-5", "effort": "low", "prompt_sha256": "x",
            "schema_version": 1, "request_id": None, "usage": {"web_search_requests": searches}, "cost_usd": cost,
            "estimate_usd": cost, "stop_reason": "end_turn", "validation": validation, "retry": False,
            "fact_ids": [], "symbols": [], "reason": reason}


def _ledger(scout_dir: Path, clock: ManualClock, **over: object) -> tuple[Ledger, ScoutConfig, ScoutPaths]:
    cfg = make_config(scout_dir, llm_enabled=True, **over)
    paths = ScoutPaths.from_config(cfg)
    return Ledger(paths, cfg, clock), cfg, paths


def test_price_from_usage(scout_dir: Path, clock: ManualClock) -> None:
    led, cfg, _ = _ledger(scout_dir, clock)
    use = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000,
           "cache_read_input_tokens": 1_000_000, "web_search_requests": 10}
    assert led.price(use, "claude-sonnet-5-5") == pytest.approx(2.0 + 10.0 + 2.5 + 0.2 + 0.1)
    assert led.price(use, "claude-haiku-5-5") == pytest.approx(0.1 + 0.5 + 0.125 + 0.01 + 0.1)


def test_estimate_is_worst_case(scout_dir: Path, clock: ManualClock) -> None:
    led, cfg, _ = _ledger(scout_dir, clock)
    req = build_j1([fact("ABC", "t", T)], cfg)
    in_tok = req.input_chars() / CHARS_PER_TOKEN
    assert led.estimate(req) == pytest.approx((in_tok * 0.125 + 4000 * 0.5) / 1e6)
    j3 = build_j3("ABC", None, [], cfg)
    # the tool loop: 3 searches + 3 fetches = 6 more iterations, each reading the prompt again and
    # every result so far (a result is the larger of a fetch's 8000 and a search's 10000 tokens)
    base = j3.input_chars() / CHARS_PER_TOKEN
    in3 = 7 * base + 10_000 * 6 * 7 / 2
    assert led.estimate(j3) == pytest.approx((in3 * 2.5 + 4000 * 10.0) / 1e6 + 3 * 0.01)


def test_the_j3_estimate_covers_a_long_tool_loop(scout_dir: Path, clock: ManualClock) -> None:
    """Regression: the estimate counted the fetch content once and no search results, while a server
    tool loop bills the growing context on every iteration: with 3 searches and 3 fetches of 8000
    tokens re-read over 7 iterations a real call could bill several times the $0.13 estimate, so a
    call allowed at $0.90 of a $1.00 cap ended the day well past it."""
    led, cfg, _ = _ledger(scout_dir, clock)
    j3 = build_j3("ABC", None, [], cfg)
    base = j3.input_chars() / CHARS_PER_TOKEN
    pages = 3 * 8000 + 3 * 10_000
    loop = sum(base + pages * i / 6 for i in range(7))  # each iteration re-reads all results so far
    real = (loop * 2.5 + 4000 * 10.0) / 1e6 + 3 * 0.01
    assert led.estimate(j3) >= real
    write_records(scout_dir, "llm", [_call("2026-11-02T12:00:00Z", cost=0.90)])
    assert led.allow("J3", j3) == (False, "daily_cap")


def test_model_without_price_is_refused(scout_dir: Path, clock: ManualClock) -> None:
    led, cfg, _ = _ledger(scout_dir, clock, llm={"jobs": {"J1": {"model": "claude-unpriced-9"}}})
    ok, reason = led.allow("J1", build_j1([fact("ABC", "t", T)], cfg))
    assert (ok, reason) == (False, "no_price")


def test_daily_and_monthly_caps(scout_dir: Path, clock: ManualClock) -> None:
    led, cfg, paths = _ledger(scout_dir, clock)
    req = build_j1([fact("ABC", "t", T)], cfg)
    assert led.allow("J1", req) == (True, "ok")
    write_records(scout_dir, "llm", [_call("2026-11-02T12:00:00Z", cost=cfg.llm_daily_cost_cap_usd - led.estimate(req) / 2)])
    assert led.allow("J1", req) == (False, "daily_cap")
    assert led.spent_today() == pytest.approx(cfg.llm_daily_cost_cap_usd - led.estimate(req) / 2)

    led_m, cfg_m, paths_m = _ledger(_fresh(scout_dir, "m"), clock, llm_monthly_cost_cap_usd=5.0)
    write_records(paths_m.root, "llm", [_call(f"2026-11-01T1{i}:00:00Z", cost=0.9999) for i in range(5)]
                  + [_call("2026-10-30T12:00:00Z", cost=50.0)])  # last month does not count
    assert led_m.spent_month() == pytest.approx(4.9995) and led_m.spent_today() == 0.0
    assert led_m.allow("J1", build_j1([fact("ABC", "t", T)], cfg_m)) == (False, "monthly_cap")


def _fresh(base: Path, name: str) -> Path:
    d = base.parent / name
    d.mkdir()
    ScoutPaths(d).ensure()
    return d


def test_job_caps(scout_dir: Path, clock: ManualClock) -> None:
    led, cfg, _ = _ledger(scout_dir, clock, j1_max_calls_per_day=2, j3_max_calls_per_day=1, web_search_max_per_day=4)
    j1, j3 = build_j1([fact("ABC", "t", T)], cfg), build_j3("ABC", None, [], cfg)
    write_records(scout_dir, "llm", [_call("2026-11-02T12:00:00Z"), _call("2026-11-02T12:01:00Z", validation="refusal"),
                                     _call("2026-11-02T12:02:00Z", validation="no_key")])  # no_key was never sent
    assert led.calls_today("J1") == 2 and led.allow("J1", j1) == (False, "j1_cap")
    assert led.allow("J3", j3) == (True, "ok")
    write_records(scout_dir, "llm", [_call("2026-11-02T12:03:00Z", job="J3", searches=2)])
    assert led.searches_today() == 2
    assert led.allow("J3", j3) == (False, "j3_cap")


def test_web_search_cap(scout_dir: Path, clock: ManualClock) -> None:
    led, cfg, _ = _ledger(scout_dir, clock, web_search_max_per_day=4)
    write_records(scout_dir, "llm", [_call("2026-11-02T12:03:00Z", job="J3", searches=2)])
    assert led.allow("J3", build_j3("ABC", None, [], cfg)) == (False, "web_search_cap")  # 2 + 3 > 4


def test_cap_reached_scope(scout_dir: Path, clock: ManualClock) -> None:
    led, cfg, _ = _ledger(scout_dir, clock)
    assert not led.cap_reached()
    write_records(scout_dir, "llm", [_call("2026-11-02T12:00:00Z", validation="refused_by_cap", reason="j1_cap")])
    assert led.cap_reached("J1") and not led.cap_reached("J3") and not led.cap_reached()
    write_records(scout_dir, "llm", [_call("2026-11-02T12:01:00Z", validation="refused_by_cap", reason="daily_cap")])
    assert led.cap_reached() and led.cap_reached("J3") and led.cap_reached("J4")
    clock.advance(86400)  # a new ET day: the jobs start again
    assert not led.cap_reached("J1")
    assert led.status()["cap_reached"] is False


def test_disabled_and_job_off(scout_dir: Path, clock: ManualClock, cfg: ScoutConfig) -> None:
    led = Ledger(ScoutPaths.from_config(cfg), cfg, clock)
    assert led.allow("J1", build_j1([fact("A", "t", T)], cfg)) == (False, "llm_disabled")
    led2, cfg2, _ = _ledger(_fresh(scout_dir, "off"), clock, llm={"jobs": {"J1": {"enabled": False}}})
    assert led2.allow("J1", build_j1([fact("A", "t", T)], cfg2)) == (False, "job_disabled")


def test_at_the_cap_layer1_and_answers_carry_on(scout_dir: Path, clock: ManualClock) -> None:
    led, cfg, paths = _ledger(scout_dir, clock)
    write_records(scout_dir, "facts", [fact("ABC", "ABC wins contract", T)])
    write_records(scout_dir, "llm", [_call("2026-11-02T12:00:00Z", cost=cfg.llm_daily_cost_cap_usd)])
    tr = FakeAnthropic(j1_reply(("F1", label("ABC", ["F1"]))))
    ran: list[str] = []
    j1 = J1Job(cfg, paths, tr, led, clock)
    jobs = [EveryJob("edgar_atom", lambda now: ran.append("layer1"), 60),
            EveryJob("answers_pilot", lambda now: ran.append("answers"), 60),
            EveryJob("llm_j1", j1.run, 120)]
    names = run_due(jobs, clock.now(), logging.getLogger("test"))
    assert names == ["edgar_atom", "answers_pilot", "llm_j1"] and ran == ["layer1", "answers"]
    assert tr.calls == [] and led.cap_reached()
    assert j1.run(clock.now()) == 0 and tr.calls == []
    assert date(2026, 11, 2) == clock.now().date()
