"""scout_reader.macro: market_context (SCOUT_SPEC 7.3, 7.6, 2 rule 9, 14; DESIGN 3.8). Fail closed,
no look-ahead, never raises."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import pytest
from helpers_fakes import FakeFetcher, write_records
from helpers_macro import BLS_URL, HALTS_URL, VIX_URL, FakeCtx, halts_body, macro_body, nowflation_results

from scout.clock import iso
from scout.jobs_macro import build_jobs
from scout.paths import ScoutPaths
from scout.scheduler import run_due
from scout_reader import records as R
from scout_reader.macro import FIELDS, load_macro_history, market_context_at, market_context_from

OK = {"nowflation": "ok", "bls": "ok", "cboe_vix": "ok", "nasdaq_halts": "ok"}


def _beat(at: datetime, sources: dict | None = None) -> dict:
    return {"format": 1, "at": iso(at), "stopped": False, "sources": dict(OK if sources is None else sources),
            "gap_open": {}}


def _state(cfg, clock, et, results: dict | None = None, day: str = "2026-11-02") -> FakeCtx:
    """Run the macro jobs once at 09:00 ET on `day` into the test Scout folder; write params."""
    now = et(day, "09:00")
    clock.set(now)
    res = {HALTS_URL: halts_body(), BLS_URL: macro_body("bls_SYNTHETIC.ics"),
           VIX_URL: macro_body("VIX_History_head_SYNTHETIC.csv"), **nowflation_results()}
    if results:
        res.update(results)
    ctx = FakeCtx(cfg=cfg, paths=ScoutPaths.from_config(cfg), clock=clock, fetch=FakeFetcher(res))
    run_due(build_jobs(ctx), now, logging.getLogger("t"))
    write_records(cfg.scout_dir, "params", [{"first_seen_at": "2026-11-01T00:00:00Z", "config_sha256": "x",
                                              **cfg.reader_params()}])
    return ctx


def _ctx_at(cfg, at: datetime, sources: dict | None = None, **kw) -> dict:
    write_records(cfg.scout_dir, "beats", [_beat(at - timedelta(seconds=15), sources)])
    return market_context_at(cfg.scout_dir, at, **kw)


def test_fresh_context(cfg, clock, et) -> None:
    _state(cfg, clock, et)
    at = et("2026-11-02", "10:00")
    out = _ctx_at(cfg, at)
    assert set(out) == set(FIELDS)
    # window [10:00 ET, Tue 09:30 ET): no release; nowflation fresh
    assert out["macro_event_next_session"]["value"] == "false"
    nxt = out["next_macro_event"]
    assert nxt["value"] == "true" and nxt["event"]["kind"] == "nfp"
    assert nxt["event"]["event_at"] == "2026-11-06T13:30:00Z" and nxt["event"]["event_at_et"] == "2026-11-06 08:30"
    reg = out["inflation_regime"]
    assert reg["value"] == "true" and reg["regime"] == "hot" and reg["attribution"] == "Nowflation https://nowflation.com"
    vix = out["vix"]
    assert vix["value"] == "true" and (vix["close"], vix["date"]) == (17.12, "2026-10-30")
    assert all(r["stale"] is False for r in out.values())


def test_event_inside_the_window_is_true_even_from_a_stale_record(cfg, clock, et) -> None:
    _state(cfg, clock, et)
    at = et("2026-11-05", "16:00")  # window to Fri 09:30 ET holds NFP 08:30
    out = _ctx_at(cfg, at)
    res = out["macro_event_next_session"]
    assert res["value"] == "true" and res["evidence"][0].startswith("nfp@2026-11-06T13:30:00Z")
    assert out["inflation_regime"]["value"] == "unknown"  # generated 11-02: stale by 11-05
    assert out["vix"]["value"] == "unknown"  # Friday's close: stale after Monday's read time


def test_fallback_to_bls_and_fomc_when_nowflation_is_stale(cfg, clock, et) -> None:
    _state(cfg, clock, et)
    at = et("2026-11-03", "10:00")  # nowflation stale since 09:15 ET; BLS fresh; table runs to 12-09
    res = _ctx_at(cfg, at)["macro_event_next_session"]
    assert res["value"] == "false" and "fallback:bls+fomc" in res["reasons"]
    assert "stale:nowflation" in res["reasons"]


def test_nowflation_stale_and_no_bls_is_unknown(cfg, clock, et) -> None:
    from helpers_fakes import fetch_result  # noqa: PLC0415

    _state(cfg, clock, et, {BLS_URL: fetch_result("bls", BLS_URL, None, result="http_error", status=500)})
    res = _ctx_at(cfg, et("2026-11-03", "10:00"))["macro_event_next_session"]
    assert res["value"] == "unknown" and "not_ok:bls" in res["reasons"]


@pytest.mark.parametrize("rt", ["realtime_run_not_ok_SYNTHETIC.json", "realtime_last_good_held_SYNTHETIC.json",
                                "realtime_not_live_SYNTHETIC.json", "realtime_stale_weight_SYNTHETIC.json"])
def test_nowflation_fail_closed_gives_unknown_regime(cfg, clock, et, rt) -> None:
    _state(cfg, clock, et, nowflation_results(rt=rt))
    out = _ctx_at(cfg, et("2026-11-02", "10:00"))
    assert out["inflation_regime"]["value"] == "unknown" and out["inflation_regime"]["regime"] == "unknown"
    # the BLS file and the FOMC table still let "no event" be said
    assert out["macro_event_next_session"]["value"] == "false"


def test_schema_drift_gives_unknown_regime(cfg, clock, et) -> None:
    _state(cfg, clock, et, nowflation_results(cal="release_calendar_drift_SYNTHETIC.json", ics=None))
    out = _ctx_at(cfg, et("2026-11-02", "10:00"))
    assert out["inflation_regime"]["value"] == "unknown"


def test_source_not_ok_in_the_beat(cfg, clock, et) -> None:
    _state(cfg, clock, et)
    out = _ctx_at(cfg, et("2026-11-02", "10:00"), {**OK, "nowflation": "stale", "cboe_vix": "unavailable"})
    assert out["inflation_regime"]["reasons"] == ["source_not_ok:nowflation"]
    assert out["vix"]["value"] == "unknown"
    assert "fallback:bls+fomc" in out["macro_event_next_session"]["reasons"]


def test_stale_scout_is_unknown_everywhere(cfg, clock, et) -> None:
    _state(cfg, clock, et)
    out = market_context_at(cfg.scout_dir, et("2026-11-02", "10:00"))  # no beat at all
    assert all(r["value"] == "unknown" and r["stale"] and r["reasons"] == ["scout_stale"] for r in out.values())
    assert out["inflation_regime"]["regime"] == "unknown" and out["next_macro_event"]["event"] is None


def test_no_params_and_naive_as_of(cfg, clock, et) -> None:
    hist = load_macro_history(cfg.scout_dir)
    at = et("2026-11-02", "10:00")
    assert all(r["reasons"] == ["no_params"] for r in market_context_from(hist, [], None, at, beat=_beat(at)).values())
    out = market_context_at(cfg.scout_dir, datetime(2026, 11, 2, 15))
    assert all(r["reasons"] == ["naive_as_of"] for r in out.values())


def test_no_look_ahead(cfg, clock, et) -> None:
    _state(cfg, clock, et)
    at = et("2026-11-02", "08:59")  # one minute before the first read
    out = _ctx_at(cfg, at)
    assert out["inflation_regime"]["value"] == "unknown" and out["vix"]["value"] == "unknown"
    res = out["macro_event_next_session"]
    assert res["value"] == "unknown" and "no_record:nowflation" in res["reasons"]


def test_calendar_not_covered(cfg, clock, et) -> None:
    _state(cfg, clock, et)
    hist = load_macro_history(cfg.scout_dir)
    at = et("2027-12-31", "17:00")
    params = R.last_params(cfg.scout_dir, at)
    out = market_context_from(hist, [], params, at, beat=_beat(at))
    assert out["macro_event_next_session"]["reasons"] == ["calendar_not_covered"]


def test_fomc_from_params_counts(cfg, clock, et) -> None:
    _state(cfg, clock, et)
    hist = load_macro_history(cfg.scout_dir)
    at = et("2026-12-08", "17:00")  # window to 12-09 09:30 ET: the 14:00 decision is after it
    params = R.last_params(cfg.scout_dir, at)
    assert market_context_from(hist, [], params, at, beat=_beat(at))["macro_event_next_session"]["value"] != "true"
    at = et("2026-12-09", "10:00")  # window to 12-10 09:30 ET holds 14:00 FOMC and the 12-10 CPI
    res = market_context_from(hist, [], params, at, beat=_beat(at))["macro_event_next_session"]
    assert res["value"] == "true" and any(e.startswith("fomc@2026-12-09T19:00:00Z(fomc)") for e in res["evidence"])


def test_macro_event_facts_add_evidence(cfg, clock, et) -> None:
    at = et("2026-11-02", "10:00")
    fact = {"format": 1, "kind": "macro_event", "event_at": iso(at + timedelta(hours=2)), "event_kind": "gdp",
            "source": "nowflation", "first_seen_at": iso(at - timedelta(hours=1)), "published_at": None}
    params = {"fomc_dates": [], "fomc_decision_time_et": "14:00"}
    res = market_context_from([], [fact], params, at, beat=_beat(at))["macro_event_next_session"]
    assert res["value"] == "true"
    later = {**fact, "first_seen_at": iso(at + timedelta(minutes=1))}  # not yet seen at as_of
    assert market_context_from([], [later], params, at, beat=_beat(at))["macro_event_next_session"]["value"] == "unknown"


def test_corrupt_unknown_format_and_partial_lines(cfg, clock, et) -> None:
    _state(cfg, clock, et)
    path = cfg.scout_dir / R.MACRO_HISTORY
    lines = path.read_text("utf-8").splitlines()
    bad = json.loads(lines[-1])
    bad["format"] = 99
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{not json\n" + json.dumps(bad) + "\n" + '{"format": 1, "source": "nowflation"')
    out = _ctx_at(cfg, et("2026-11-02", "10:00"))
    assert out["inflation_regime"]["regime"] == "hot"


def test_never_raises(cfg, et) -> None:
    class Boom:
        def __iter__(self):
            raise OSError("gone")

    at = et("2026-11-02", "10:00")
    out = market_context_from(Boom(), [], {"fomc_dates": []}, at, beat=_beat(at))
    assert all(r["value"] == "unknown" and r["reasons"] == ["error:OSError"] for r in out.values())
    junk = [None, 1, "x", {"format": 1, "source": "nowflation"}, {"format": 1, "source": "bls", "first_seen_at": "x"}]
    out = market_context_from(junk, None, {"fomc_dates": ["bad"], "fomc_decision_time_et": "noon"}, at,
                              beat=_beat(at))
    assert out["macro_event_next_session"]["value"] == "unknown"
    assert market_context_at(cfg.scout_dir / "missing", at)["vix"]["value"] == "unknown"


def test_record_without_stale_after_is_not_fresh(cfg, et) -> None:
    at = et("2026-11-02", "10:00")
    rec = {"format": 1, "source": "nowflation", "first_seen_at": iso(at - timedelta(hours=1)), "status": "ok",
           "inflation_regime": "hot", "events": []}
    out = market_context_from([rec], [], {"fomc_dates": []}, at, beat=_beat(at))
    assert out["inflation_regime"]["value"] == "unknown" and out["inflation_regime"]["reasons"] == ["stale:nowflation"]
