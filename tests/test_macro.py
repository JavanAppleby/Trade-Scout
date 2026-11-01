"""Merge and cross-checks, inflation regime, forecast lean, stale_after and the macro writer
(SCOUT_SPEC 7.3, 7.6, 10.2; DESIGN 2.3, 5.8)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_config
from helpers_fakes import FakeFactSink
from helpers_macro import macro_body

from scout.bls_ics import bls_events, parse_bls_ics
from scout.clock import ManualClock
from scout.fomc import fomc_events, fomc_record
from scout.macro import (ATTRIBUTION, MacroEvent, MacroWriter, events_from_dicts, fact_id, forecast_lean,
                         inflation_regime, merge_events, normalise_kind, stale_after)
from scout.models import MacroRecord
from scout.nowflation import parse_forecasts, parse_realtime, parse_release_calendar

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)


def _now_events(cfg):
    c = parse_release_calendar(macro_body("release_calendar_SYNTHETIC.json"), fomc_time=cfg.fomc_decision_time_et)
    return [*c.events, *c.fomc]


def _bls():
    return bls_events(parse_bls_ics(macro_body("bls_SYNTHETIC.ics")))[0]


def _ev(kind: str, at: datetime, source: str) -> MacroEvent:
    return MacroEvent(kind=kind, event_at=at, source=source)


def test_normalise_kind() -> None:
    assert [normalise_kind(k) for k in ("CPI", " NFP ", "Jobs", "PCE", "FOMC", "GDP", "", None)] == [
        "cpi", "nfp", "nfp", "pce", "fomc", "gdp", "other", "other"]


# --------------------------------------------------------------------------- merge


def test_merge_cross_checks(cfg) -> None:
    merged = merge_events(_now_events(cfg), _bls(), fomc_events(cfg), cfg)
    by = {(e.kind, e.event_at.date().isoformat()): e for e in merged}
    cpi = by[("cpi", "2026-11-12")]
    assert cpi.sources == ("nowflation", "bls") and cpi.sources_agree is True
    assert cpi.prior == 0.3 and cpi.nowflation_forecast == 0.2  # nowflation's values kept
    assert by[("nfp", "2026-11-06")].sources_agree is True
    assert by[("pce", "2026-11-25")].sources_agree is None  # no second source in v1
    fomc = by[("fomc", "2026-12-09")]
    assert fomc.sources == ("nowflation", "fomc") and fomc.sources_agree is True
    # beyond nowflation's listing: no claim either way
    assert by[("cpi", "2026-12-10")].sources_agree is None and by[("cpi", "2026-12-10")].sources == ("bls",)
    assert by[("fomc", "2026-10-28")].sources_agree is None
    assert [e.event_at for e in merged] == sorted(e.event_at for e in merged)


def test_times_within_the_agree_minutes_agree(cfg) -> None:
    at = datetime(2026, 11, 12, 13, 30, tzinfo=UTC)
    near = merge_events([_ev("cpi", at, "nowflation")], [_ev("cpi", at + timedelta(minutes=4), "bls")], [], cfg)
    assert len(near) == 1 and near[0].sources_agree is True and near[0].event_at == at


def test_disagreement_takes_the_earlier_time(cfg) -> None:
    at = datetime(2026, 11, 12, 13, 30, tzinfo=UTC)
    late = merge_events([_ev("cpi", at + timedelta(minutes=15), "nowflation")], [_ev("cpi", at, "bls")], [], cfg)
    assert len(late) == 1 and late[0].sources_agree is False and late[0].event_at == at
    assert late[0].times == {"nowflation": "2026-11-12T13:45:00Z", "bls": "2026-11-12T13:30:00Z"}
    day = merge_events([_ev("cpi", at + timedelta(days=1), "nowflation")], [_ev("cpi", at, "bls")], [], cfg)
    assert len(day) == 1 and day[0].sources_agree is False and day[0].event_at == at


def test_agree_minutes_come_from_config(scout_dir) -> None:
    wide = make_config(scout_dir, macro_agree_minutes=20)
    at = datetime(2026, 11, 12, 13, 30, tzinfo=UTC)
    out = merge_events([_ev("cpi", at + timedelta(minutes=15), "nowflation")], [_ev("cpi", at, "bls")], [], wide)
    assert out[0].sources_agree is True and out[0].event_at == at


def test_unmatched_event_disagrees_only_when_the_other_source_lists_that_time(cfg) -> None:
    at = datetime(2026, 11, 12, 13, 30, tzinfo=UTC)
    nfp_only = [_ev("nfp", at - timedelta(days=6), "bls"), _ev("nfp", at + timedelta(days=22), "bls")]
    out = merge_events([_ev("cpi", at, "nowflation")], [], [], cfg)
    assert out[0].sources_agree is None  # BLS not read
    out = merge_events([_ev("cpi", at, "nowflation")], [*nfp_only, _ev("cpi", at + timedelta(days=28), "bls")], [], cfg)
    cpi = [e for e in out if e.kind == "cpi" and e.source == "nowflation"][0]
    assert cpi.sources_agree is None  # BLS's next CPI is 28 days away: not the same release
    # each source lists a CPI the other's listing spans but does not match: they disagree;
    # outside the other's span: no claim
    out = merge_events([_ev("cpi", at, "nowflation"), _ev("cpi", at + timedelta(days=30), "nowflation")],
                       [_ev("cpi", at - timedelta(days=30), "bls"), _ev("cpi", at + timedelta(days=15), "bls")], [], cfg)
    assert [(e.source, e.sources_agree) for e in out] == [("bls", None), ("nowflation", False), ("bls", False),
                                                          ("nowflation", None)]


def test_fomc_is_checked_by_date(cfg) -> None:
    table = [_ev("fomc", datetime(2026, 12, 9, 19, 0, tzinfo=UTC), "fomc")]
    out = merge_events([_ev("fomc", datetime(2026, 12, 9, 19, 30, tzinfo=UTC), "nowflation")], [], table, cfg)
    assert out[0].sources_agree is True and out[0].event_at == datetime(2026, 12, 9, 19, 0, tzinfo=UTC)
    out = merge_events([_ev("fomc", datetime(2026, 12, 10, 19, 0, tzinfo=UTC), "nowflation")], [], table, cfg)
    assert out[0].sources_agree is False and out[0].event_at == datetime(2026, 12, 9, 19, 0, tzinfo=UTC)


def test_events_round_trip(cfg) -> None:
    events = _now_events(cfg)
    back = events_from_dicts([e.to_dict() for e in events] + [{"kind": "cpi"}, "junk"], "nowflation")
    assert [(e.kind, e.event_at, e.prior) for e in back] == [(e.kind, e.event_at, e.prior) for e in events]


# --------------------------------------------------------------------------- regime and lean


@pytest.mark.parametrize("change,regime", [(0.31, "hot"), (0.25, "hot"), (0.1, "steady"), (-0.24, "steady"),
                                           (-0.25, "cooling"), (-1.0, "cooling"), (None, "unknown"),
                                           ("0.3", "unknown"), (True, "unknown"), (float("nan"), "unknown")])
def test_inflation_regime(cfg, change, regime) -> None:
    assert inflation_regime({"change_20d": change}, cfg) == regime


def test_regime_window_and_thresholds_from_config(scout_dir) -> None:
    rt = parse_realtime(macro_body("realtime_SYNTHETIC.json"))  # changes 1d 0.015, 5d -0.072, 20d -0.064
    assert inflation_regime(rt, make_config(scout_dir)) == "steady"
    assert inflation_regime(rt, make_config(scout_dir, inflation_regime_window_days=5)) == "steady"
    assert inflation_regime(rt, make_config(scout_dir, inflation_regime_window_days=5, inflation_cooling_pp=0.05))         == "cooling"
    assert inflation_regime(rt, make_config(scout_dir, inflation_cooling_pp=0.005)) == "cooling"
    assert inflation_regime(rt, make_config(scout_dir, inflation_regime_window_days=10)) == "unknown"
    assert inflation_regime(None, make_config(scout_dir)) == "unknown"
    hot = parse_realtime(macro_body("realtime_SYNTHETIC.json").replace(b'"realtime_wow_pp": -0.072',
                                                                     b'"realtime_wow_pp": 0.3'))
    assert inflation_regime(hot, make_config(scout_dir, inflation_regime_window_days=5)) == "hot"


def test_forecast_lean_is_informational() -> None:
    cal = parse_release_calendar(macro_body("release_calendar_SYNTHETIC.json"))
    fc = parse_forecasts(macro_body("forecasts_index_SYNTHETIC.json"))
    lean = forecast_lean(cal, fc)
    assert lean == [{"kind": "cpi", "event_at": "2026-11-12T13:30:00Z", "vs_prior": -0.1, "vs_street": None,
                     "graded_prints": 3, "note": "informational, untested", "forecast": 0.2, "prior": 0.3,
                     "street": None, "vs_cleveland": -0.05, "print_month": None, "receipt_status": None}]
    assert forecast_lean(None) == []
    ev = MacroEvent(kind="cpi", event_at=NOW, source="nowflation", prior={"headline": 0.3, "core": 0.2},
                    nowflation_forecast={"headline": 0.2, "core": 0.3, "x": "a"}, street=0.1)
    out = forecast_lean(type("C", (), {"events": [ev]})())
    assert out[0]["vs_prior"] == {"core": 0.1, "headline": -0.1} and out[0]["vs_street"] is None


def test_forecast_lean_takes_missing_values_from_the_receipt_for_the_print_month() -> None:
    fc = parse_forecasts(macro_body("forecasts_index_SYNTHETIC.json"))
    # a September CPI event with only a prior: forecast 2.83, street 2.8, Cleveland 2.86 from the receipt
    ev = MacroEvent(kind="cpi", event_at=NOW, source="nowflation", prior=2.7, extra={"data_month": "2026-09"})
    (lean,) = forecast_lean(type("C", (), {"events": [ev]})(), fc)
    assert (lean["forecast"], lean["vs_prior"], lean["vs_street"], lean["vs_cleveland"]) == (2.83, 0.13, 0.03, -0.03)
    assert lean["receipt_status"] == "locked" and lean["print_month"] == "2026-09"
    # the calendar's own values win over the receipt's
    ev2 = MacroEvent(kind="cpi", event_at=NOW, source="nowflation", prior=3.4, nowflation_forecast=3.5, street=3.5,
                     extra={"data_month": "2026-09"})
    (lean2,) = forecast_lean(type("C", (), {"events": [ev2]})(), fc)
    assert (lean2["forecast"], lean2["vs_street"]) == (3.5, 0.0)
    # no receipt for the month and no calendar forecast: left out; non-CPI kinds never use receipts
    none = MacroEvent(kind="cpi", event_at=NOW, source="nowflation", prior=3.4, extra={"data_month": "2025-01"})
    pce = MacroEvent(kind="pce", event_at=NOW, source="nowflation", prior=3.4, extra={"data_month": "2026-09"})
    assert forecast_lean(type("C", (), {"events": [none, pce]})(), fc) == []


# --------------------------------------------------------------------------- stale_after


def _rec(source: str, **kw) -> MacroRecord:
    base = dict(source=source, first_seen_at="2026-11-02T14:00:00Z", fetched_at="2026-11-02T14:00:00Z", status="ok",
                reasons=(), source_generated_at=None, sha256=None, events=(), realtime=None, inflation_regime=None,
                forecast_lean=(), vix=None, attribution=None)
    base.update(kw)
    return MacroRecord(**base)


def test_stale_after(cfg) -> None:
    # generated 06:05 ET on 11-02: stale at 09:15 ET on 11-03 (before 30 h)
    assert stale_after(_rec("nowflation", source_generated_at="2026-11-02T11:05:00Z"), cfg) == "2026-11-03T14:15:00Z"
    # generated 23:00 ET: 30 h comes first? no: next day's 09:15 ET is 10 h 15 min later
    assert stale_after(_rec("nowflation", source_generated_at="2026-11-03T04:00:00Z"), cfg) == "2026-11-03T14:15:00Z"
    assert stale_after(_rec("bls"), cfg) == "2026-11-16T14:00:00Z"
    assert stale_after(_rec("cboe_vix"), cfg) == "2026-11-02T22:30:00Z"  # 16:30 ET read + 1 h
    friday = _rec("cboe_vix", fetched_at="2026-10-30T20:35:00Z")  # Fri 16:35 ET
    assert stale_after(friday, cfg) == "2026-11-02T22:30:00Z"  # Monday's read + 1 h
    assert stale_after(_rec("fomc"), cfg) == "2026-12-09T19:00:00Z"
    assert stale_after(_rec("nowflation", status="stale"), cfg) is None
    assert stale_after(_rec("other"), cfg) is None


def test_stale_after_max_age(scout_dir) -> None:
    tight = make_config(scout_dir, nowflation_max_age_h=2)
    assert stale_after(_rec("nowflation", source_generated_at="2026-11-02T11:05:00Z"), tight) == "2026-11-02T13:05:00Z"


def test_fact_id_formula() -> None:
    assert fact_id("bls", "g", None) == hashlib.sha256(b"bls|g|").hexdigest()
    assert fact_id("nasdaq_halts", "X|d|t", "X") == hashlib.sha256(b"nasdaq_halts|X|d|t|X").hexdigest()


# --------------------------------------------------------------------------- writer


def _now_record(cfg) -> MacroRecord:
    cal = parse_release_calendar(macro_body("release_calendar_SYNTHETIC.json"), fomc_time=cfg.fomc_decision_time_et)
    rt = parse_realtime(macro_body("realtime_SYNTHETIC.json"))
    return _rec("nowflation", source_generated_at="2026-11-02T11:05:00Z", inflation_regime="hot",
                events=tuple(e.to_dict() for e in sorted([*cal.events, *cal.fomc], key=lambda e: e.event_at)),
                realtime=rt.to_record(), attribution=ATTRIBUTION)


def test_writer_files_and_facts(cfg, paths, write_audit) -> None:
    clock = ManualClock(NOW)
    facts = FakeFactSink()
    writer = MacroWriter(paths, facts, cfg, clock)
    with write_audit.watching(paths.root):
        writer.write(fomc_record(cfg, NOW))
        writer.write(_rec("bls", events=tuple(e.to_dict() for e in _bls())))
        n = writer.write(_now_record(cfg))
        writer.write(_rec("cboe_vix", vix={"date": "2026-10-30", "close": 17.12}))
    history = [json.loads(x) for x in (paths.macro / "history.jsonl").read_text("utf-8").splitlines()]
    assert [h["source"] for h in history] == ["fomc", "bls", "nowflation", "cboe_vix"]
    assert all(h["format"] == 1 and "stale_after" in h for h in history)
    assert history[2]["stale_after"] == "2026-11-03T14:15:00Z"
    daily = json.loads((paths.macro / "2026-11-02.json").read_text("utf-8"))
    assert daily["format"] == 1 and daily["inflation_regime"] == "hot" and daily["regime_status"] == "ok"
    assert daily["vix"] == {"date": "2026-10-30", "close": 17.12}
    assert daily["attribution"] == "Nowflation https://nowflation.com"
    assert set(daily["sources"]) == {"fomc", "bls", "nowflation", "cboe_vix"}
    cpi = [e for e in daily["events"] if e["kind"] == "cpi" and e["event_at"] == "2026-11-12T13:30:00Z"][0]
    assert cpi["sources"] == ["nowflation", "bls"] and cpi["sources_agree"] is True
    assert n > 0
    kinds = {f.kind for f in facts.facts}
    assert kinds == {"macro_event"}
    cpi_fact = [f for f in facts.facts if f.event_kind == "cpi" and f.event_at == "2026-11-12T13:30:00Z"
                and f.event_source == "nowflation+bls"][0]
    assert cpi_fact.sources_agree is True and cpi_fact.extra["attribution"] == ATTRIBUTION
    assert cpi_fact.ticker is None and cpi_fact.first_seen_at == "2026-11-02T14:00:00Z"
    assert all(datetime.fromisoformat(f.event_at.replace("Z", "+00:00")) >= NOW for f in facts.facts)


def test_writer_adds_no_duplicate_facts(cfg, paths) -> None:
    facts = FakeFactSink()
    writer = MacroWriter(paths, facts, cfg, ManualClock(NOW))
    writer.write(_now_record(cfg))
    before = len(facts.facts)
    assert writer.write(_now_record(cfg)) == 0 and len(facts.facts) == before


def test_writer_regime_unknown_when_nowflation_not_fresh(cfg, paths) -> None:
    clock = ManualClock(NOW)
    writer = MacroWriter(paths, FakeFactSink(), cfg, clock)
    writer.write(_rec("nowflation", status="stale", reasons=("run_not_ok",), inflation_regime="unknown"))
    daily = json.loads((paths.macro / "2026-11-02.json").read_text("utf-8"))
    assert daily["inflation_regime"] == "unknown" and daily["regime_status"] == "stale"
    clock.set(NOW + timedelta(days=2))
    writer.write(_now_record(cfg))  # generated 11-02: no longer fresh on 11-04
    daily = json.loads((paths.macro / "2026-11-04.json").read_text("utf-8"))
    assert daily["inflation_regime"] == "unknown"


def test_writer_reloads_history_after_a_restart(cfg, paths) -> None:
    MacroWriter(paths, FakeFactSink(), cfg, ManualClock(NOW)).write(_now_record(cfg))
    again = MacroWriter(paths, FakeFactSink(), cfg, ManualClock(NOW + timedelta(minutes=5)))
    assert set(again.latest) == {"nowflation"}
    again.write(_rec("cboe_vix", vix={"date": "2026-10-30", "close": 17.12}))
    daily = json.loads((paths.macro / "2026-11-02.json").read_text("utf-8"))
    assert daily["inflation_regime"] == "hot" and set(daily["sources"]) == {"nowflation", "cboe_vix"}
