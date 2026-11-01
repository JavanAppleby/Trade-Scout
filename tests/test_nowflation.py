"""nowflation.com: pinned schemas, the SCOUT_SPEC 7.3 fail-closed list and the reader
(SCOUT_SPEC 4.1 #7, 7.3, 14; DESIGN 2.3)."""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from conftest import make_config
from helpers_fakes import FakeBoard, FakeFetcher, FakeHealthSink, FakeRawSink, fetch_result
from helpers_macro import CAL_URL, FC_URL, ICS_URL, RT_URL, macro_body, nowflation_results

from scout.macro import ATTRIBUTION, SchemaError
from scout.nowflation import (NowflationReader, freshness, is_release_day, parse_forecasts, parse_ics,
                              parse_realtime, parse_release_calendar)

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)  # Monday 09:00 ET


def cal(name: str = "release_calendar_SYNTHETIC.json"):
    return parse_release_calendar(macro_body(name))


def rt(name: str = "realtime_SYNTHETIC.json"):
    return parse_realtime(macro_body(name))


def _mutate(name: str, fn) -> bytes:
    obj = json.loads(macro_body(name))
    fn(obj)
    return json.dumps(obj).encode()


# --------------------------------------------------------------------------- release calendar


def test_release_calendar_events_use_datetime_utc() -> None:
    c = cal()
    assert c.generated_at == datetime(2026, 11, 2, 11, 5, tzinfo=UTC)
    assert c.from_ == "2026-11-02" and c.past_count == 1
    by = {e.kind: e for e in c.events}
    assert sorted(by) == ["cpi", "nfp", "pce"]
    assert by["cpi"].event_at == datetime(2026, 11, 12, 13, 30, tzinfo=UTC)
    assert by["nfp"].event_at == datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    assert [f.event_at for f in c.fomc] == [datetime(2026, 12, 9, 0, 0, tzinfo=UTC)]  # date only, no time given


def test_fomc_date_takes_the_configured_decision_time(cfg) -> None:
    c = parse_release_calendar(macro_body("release_calendar_SYNTHETIC.json"), fomc_time=cfg.fomc_decision_time_et)
    assert [f.event_at for f in c.fomc] == [datetime(2026, 12, 9, 19, 0, tzinfo=UTC)]


def test_optional_values_are_stored_when_present_and_never_assumed() -> None:
    by = {e.kind: e for e in cal().events}
    assert (by["cpi"].prior, by["cpi"].nowflation_forecast, by["cpi"].cleveland) == (0.3, 0.2, 0.25)
    assert by["cpi"].street is None and by["cpi"].kalshi is None
    assert (by["nfp"].prior, by["nfp"].street, by["nfp"].nowflation_forecast) == (140, 120, None)
    assert (by["pce"].kalshi, by["pce"].cleveland) == (0.3, None)


def test_et_text_is_never_parsed() -> None:
    body = _mutate("release_calendar_SYNTHETIC.json",
                   lambda o: [e.update(datetime_et="garbage 99:99") for e in o["events"]])
    assert [e.event_at for e in parse_release_calendar(body).events] == [e.event_at for e in cal().events]


def test_extra_keys_are_allowed() -> None:
    body = _mutate("release_calendar_SYNTHETIC.json", lambda o: o.update(new_top_level_key={"a": 1}))
    assert len(parse_release_calendar(body).events) == 3


@pytest.mark.parametrize("fn", [
    lambda o: o.pop("generated_at"), lambda o: o.pop("past"), lambda o: o.pop("fomc"), lambda o: o.pop("from"),
    lambda o: o.pop("source"), lambda o: o.update(events={"cpi": 1}),
    lambda o: o["events"][0].pop("kind"), lambda o: o["events"][0].update(kind=" "),
    lambda o: o["events"][0].update(datetime_utc="2026-11-12 08:30"),  # naive
    lambda o: o["events"][0].update(datetime_utc=1762954200),
    lambda o: o.update(generated_at="yesterday"), lambda o: o["past"].append("x"),
    lambda o: o["fomc"].append({"when": "soon"}), lambda o: o["fomc"].append({"date": "12/09/2026"}),
])
def test_calendar_drift_refuses_the_source(fn) -> None:
    with pytest.raises(SchemaError):
        parse_release_calendar(_mutate("release_calendar_SYNTHETIC.json", fn))


@pytest.mark.parametrize("body", [b"", b"<html>", b"[]", b"\xff\xfe", b"null"])
def test_not_json_is_drift(body) -> None:
    for parse in (parse_release_calendar, parse_realtime, parse_forecasts):
        with pytest.raises(SchemaError):
            parse(body)


def test_drift_fixture_refuses_rather_than_skips() -> None:
    with pytest.raises(SchemaError):
        cal("release_calendar_drift_SYNTHETIC.json")


# --------------------------------------------------------------------------- realtime and forecasts


def test_realtime_fields_in_the_live_layout() -> None:
    """The synthetic realtime.json in nowflation's live layout (values invented)."""
    r = rt()
    assert r.generated_at == datetime(2026, 11, 2, 11, 40, tzinfo=UTC)
    assert r.run_ok and r.checks_ok and r.record_type == "live" and not r.last_good_held
    assert r.stale_weight_pct == 4.0
    # 1 day = today.realtime_dod_pp, 5 = realtime_wow_pp, 20 = history 11-02 (2.637) - 10-05 (2.701)
    assert r.changes == {1: 0.015, 5: -0.072, 20: -0.064}
    assert r.to_record() == {"realtime_yoy_pct": 2.64, "change_1d": 0.015, "change_5d": -0.072, "change_20d": -0.064,
                             "cpi_gauge_yoy_pct": 2.81, "official_cpi_yoy_pct": 2.9, "official_cpi_month": "2026-09",
                             "live_share_pct": 52.0, "avg_input_age_days": 12.5}


def test_the_synthetic_files_carry_unknown_keys_and_still_parse() -> None:
    """Extra keys are allowed (the pinned schemas name only what Scout reads)."""
    obj = json.loads(macro_body("realtime_SYNTHETIC.json"))
    assert {"unknown_top_level", "_SYNTHETIC"} <= set(obj) and "unknown_today_key" in obj["today"]
    assert "unknown_weight_pct" in obj["freshness"]["realtime"] and "unknown_flag" in obj["freshness"]
    fc = json.loads(macro_body("forecasts_index_SYNTHETIC.json"))
    assert "unknown_count" in fc and all("unknown_receipt_key" in r for r in fc["receipts"])
    assert rt().values["realtime_yoy_pct"] == 2.64 and parse_forecasts(json.dumps(fc).encode()).graded == 3


def test_stale_weight_falls_back_to_the_stale_list() -> None:
    body = _mutate("realtime_SYNTHETIC.json", lambda o: o["freshness"]["realtime"].pop("stale_weight_pct"))
    assert parse_realtime(body).stale_weight_pct == 4.0  # component_a 4.0
    body = _mutate("realtime_SYNTHETIC.json", lambda o: (o["freshness"]["realtime"].pop("stale_weight_pct"),
                                                      o["freshness"]["realtime"].update(stale=[])))
    assert parse_realtime(body).stale_weight_pct == 0.0


@pytest.mark.parametrize("stale", [None, "x", [{"weight_pct": "3.8"}], [3.8]])
def test_stale_weight_without_a_readable_fallback_is_none(stale) -> None:
    def fn(o):
        o["freshness"]["realtime"].pop("stale_weight_pct")
        if stale is None:
            o["freshness"]["realtime"].pop("stale")
        else:
            o["freshness"]["realtime"]["stale"] = stale
    assert parse_realtime(_mutate("realtime_SYNTHETIC.json", fn)).stale_weight_pct is None


def test_the_20_day_change_needs_a_history_point_about_28_days_back() -> None:
    def drop(dates):
        return lambda o: o.update(history=[h for h in o["history"] if h["date"] not in dates])
    # the 28-day point (10-05) gone: 27 days back (10-06, 2.512) is used
    assert parse_realtime(_mutate("realtime_SYNTHETIC.json", drop({"2026-10-05"}))).changes[20] == round(2.637 - 2.512, 6)
    # nothing within 26..30 days back: no 20-day change, and the regime is unknown
    gone = parse_realtime(_mutate("realtime_SYNTHETIC.json", drop({"2026-10-03", "2026-10-04", "2026-10-05",
                                                                  "2026-10-06", "2026-10-07"})))
    assert 20 not in gone.changes and gone.to_record()["change_20d"] is None
    # no history at all: the same, and the file is still read
    assert 20 not in parse_realtime(_mutate("realtime_SYNTHETIC.json", lambda o: o.pop("history"))).changes


def test_the_20_day_change_is_never_across_a_method_switch_or_from_a_non_live_point() -> None:
    def point(**kw):
        def fn(o):
            for h in o["history"]:
                if h["date"] == "2026-10-05":
                    h.update(kw)
        return fn
    assert 20 not in parse_realtime(_mutate("realtime_SYNTHETIC.json", point(method_version="v9.0"))).changes
    assert 20 not in parse_realtime(_mutate("realtime_SYNTHETIC.json", point(record_type="reconstructed"))).changes
    # the file's own switch (v9.0 to v9.1 on 09-30): read on 10-27, the nearest point (09-29) is v9.0,
    # so there is no 20-day change; on 10-28 the nearest is 09-30 (v9.1): 2.64 (headline) - 2.598
    def on(day):
        return lambda o: o["today"].update(date=day)
    assert 20 not in parse_realtime(_mutate("realtime_SYNTHETIC.json", on("2026-10-27"))).changes
    assert parse_realtime(_mutate("realtime_SYNTHETIC.json", on("2026-10-28"))).changes[20] == round(2.64 - 2.598, 6)


def test_without_todays_history_point_the_headline_is_the_base() -> None:
    body = _mutate("realtime_SYNTHETIC.json", lambda o: o.update(history=[h for h in o["history"]
                                                                       if h["date"] != "2026-11-02"]))
    assert parse_realtime(body).changes[20] == round(2.64 - 2.701, 6)


@pytest.mark.parametrize("name", ["realtime_drift_SYNTHETIC.json", "realtime_drift_type_SYNTHETIC.json"])
def test_realtime_drift_fixtures(name) -> None:
    with pytest.raises(SchemaError):
        rt(name)


@pytest.mark.parametrize("fn", [
    lambda o: o["run"].pop("checks_ok"), lambda o: o["run"].update(ok="yes"), lambda o: o["run"].update(ok=1),
    lambda o: o.pop("today"), lambda o: o["today"].update(record_type=None),
    lambda o: o["today"].pop("record_type"), lambda o: o["today"].update(last_good_held=0),
    lambda o: o["today"].pop("last_good_held"), lambda o: o["today"].update(realtime_yoy_pct=True),
    lambda o: o["today"].pop("realtime_yoy_pct"), lambda o: o.pop("freshness"),
    lambda o: o["freshness"].pop("realtime"), lambda o: o["freshness"].update(realtime=[]),
    lambda o: o["freshness"]["realtime"].pop("live_share_pct"), lambda o: o.pop("generated_at"),
    lambda o: o.update(generated_at="yesterday"),
    # the synthetic layout of the first build (top-level last_good_held and realtime_yoy_pct) is refused
    lambda o: o.update(last_good_held=o["today"].pop("last_good_held")),
])
def test_realtime_drift(fn) -> None:
    with pytest.raises(SchemaError):
        parse_realtime(_mutate("realtime_SYNTHETIC.json", fn))


def test_a_non_finite_headline_is_drift() -> None:
    body = macro_body("realtime_SYNTHETIC.json").replace(b'"realtime_yoy_pct": 2.64', b'"realtime_yoy_pct": NaN')
    with pytest.raises(SchemaError):
        parse_realtime(body)


def test_forecasts_in_the_live_layout() -> None:
    fc = parse_forecasts(macro_body("forecasts_index_SYNTHETIC.json"))
    assert fc.graded == 3 and fc.latest == "2026-10" and len(fc.items) == 4
    oct_ = fc.receipt("2026-10")
    assert oct_["status"] == "provisional" and oct_["forecast"] == 2.71 and oct_["cleveland"] == 2.58
    assert oct_["street"] is None and oct_["kalshi"] is None and oct_["graded"] is False
    sep = fc.receipt("2026-09")
    assert (sep["status"], sep["street"], sep["actual"], sep["signed_error"]) == ("locked", 2.8, 2.9, -0.07)
    assert fc.receipt("2025-01") is None


@pytest.mark.parametrize("fn", [
    lambda o: o.pop("receipts"), lambda o: o.update(receipts={}), lambda o: o.pop("n_graded"),
    lambda o: o.update(n_graded="4"), lambda o: o.update(n_graded=True), lambda o: o.pop("generated_at"),
    lambda o: o["receipts"][0].pop("print_month"), lambda o: o["receipts"].append("2026-11"),
    lambda o: o.update(forecasts=o.pop("receipts")),  # the synthetic layout of the first build
])
def test_forecasts_drift(fn) -> None:
    with pytest.raises(SchemaError):
        parse_forecasts(_mutate("forecasts_index_SYNTHETIC.json", fn))


def test_forecasts_drift_fixture() -> None:
    with pytest.raises(SchemaError):
        parse_forecasts(macro_body("forecasts_index_drift_SYNTHETIC.json"))


def test_ics_fallback_parses() -> None:
    events = parse_ics(macro_body("nowflation_SYNTHETIC.ics"))
    assert [e.start for e in events] == [datetime(2026, 11, 12, 13, 30, tzinfo=UTC),
                                         datetime(2026, 11, 6, 13, 30, tzinfo=UTC),
                                         datetime(2026, 12, 9, 19, 0, tzinfo=UTC)]


# --------------------------------------------------------------------------- freshness (SS 7.3)


def test_fresh_files_are_ok(cfg) -> None:
    assert freshness(cal(), rt(), NOW, cfg) == ("ok", [])


@pytest.mark.parametrize("name,reason", [
    ("realtime_run_not_ok_SYNTHETIC.json", "run_not_ok"),
    ("realtime_checks_not_ok_SYNTHETIC.json", "checks_not_ok"),
    ("realtime_not_live_SYNTHETIC.json", "record_type:reconstructed"),
    ("realtime_last_good_held_SYNTHETIC.json", "last_good_held"),
    ("realtime_stale_weight_SYNTHETIC.json", "stale_weight:21.4"),
    ("realtime_all_stale_SYNTHETIC.json", "stale_weight:100.0"),
    ("realtime_stale_SYNTHETIC.json", "realtime_generated_at_old"),
])
def test_each_fail_closed_condition(cfg, name, reason) -> None:
    status, reasons = freshness(cal(), rt(name), NOW, cfg)
    assert status == "stale" and reason in reasons


def test_old_calendar_is_stale(cfg) -> None:
    status, reasons = freshness(cal("release_calendar_stale_SYNTHETIC.json"), rt(), NOW, cfg)
    assert status == "stale" and "calendar_generated_at_old" in reasons


def test_not_todays_date_after_the_fresh_after_time(cfg, et) -> None:
    # generated 2026-11-02 06:05 ET; on 11-03 before 09:15 ET it is still fresh (27 h < 30 h) ...
    assert freshness(cal(), rt(), et("2026-11-03", "09:14"), cfg)[0] == "ok"
    # ... and from 09:15 ET it is not today's date
    status, reasons = freshness(cal(), rt(), et("2026-11-03", "09:15"), cfg)
    assert status == "stale" and "calendar_generated_at_not_today" in reasons


def test_max_age_and_stale_weight_follow_config(scout_dir, et) -> None:
    tight = make_config(scout_dir, nowflation_max_age_h=2, nowflation_max_stale_weight_pct=3)
    status, reasons = freshness(cal(), rt(), NOW, tight)
    assert status == "stale" and {"calendar_generated_at_old", "stale_weight:7.1"} <= set(reasons)


def test_the_stale_weight_gate_is_the_stale_share_of_the_market_sourced_weight(cfg) -> None:
    # live 52.0, stale 4.0 -> 7.1 % of the 56.0 market-sourced weight (default 20)
    assert freshness(cal(), rt(), NOW, cfg) == ("ok", [])
    # every market source stopped: live 0, stale 56.0 (the most this file can reach, below the
    # absolute threshold of 70 the first build used) -> 100 %
    r = rt("realtime_all_stale_SYNTHETIC.json")
    assert r.values["live_share_pct"] == 0.0 and r.stale_weight_pct == 56.0
    status, reasons = freshness(cal(), r, NOW, cfg)
    assert status == "stale" and reasons == ["stale_weight:100.0"]
    assert cfg.nowflation_max_stale_weight_pct == 20.0
    # 21.4 % is stale at 20 and ok at 21.5
    edge = rt("realtime_stale_weight_SYNTHETIC.json")  # 12.0 of 56.0 = 21.4 %
    assert freshness(cal(), edge, NOW, cfg)[0] == "stale"
    loose = dataclasses.replace(cfg, nowflation_max_stale_weight_pct=21.5)
    assert freshness(cal(), edge, NOW, loose) == ("ok", [])


def test_no_market_sourced_weight_at_all_is_stale(cfg) -> None:
    # every component held from BLS (none live, none stale): the nowcast is official data only
    body = _mutate("realtime_SYNTHETIC.json", lambda o: (o["freshness"]["realtime"].update(
        live_share_pct=0, stale_weight_pct=0, stale=[])))
    assert freshness(cal(), parse_realtime(body), NOW, cfg) == ("stale", ["no_live_inputs"])


def test_missing_files_are_unavailable(cfg) -> None:
    assert freshness(None, rt(), NOW, cfg) == ("unavailable", ["no_release_calendar"])
    assert freshness(cal(), None, NOW, cfg)[0] == "unavailable"


def test_generated_in_the_future_is_stale(cfg) -> None:
    assert freshness(cal(), rt(), NOW - timedelta(hours=4), cfg)[0] == "stale"


def test_missing_stale_weight_fails_closed(cfg) -> None:
    body = _mutate("realtime_SYNTHETIC.json", lambda o: (o["freshness"]["realtime"].pop("stale_weight_pct"),
                                                      o["freshness"]["realtime"].pop("stale")))
    assert freshness(cal(), parse_realtime(body), NOW, cfg) == ("stale", ["stale_weight_missing"])
    body = _mutate("realtime_SYNTHETIC.json", lambda o: o["freshness"]["realtime"].update(live_share_pct=None))
    with pytest.raises(SchemaError):  # live_share_pct must be a number
        parse_realtime(body)


def test_the_live_layout_files_are_fresh_and_steady_together(cfg) -> None:
    """The three synthetic files in the live layout, read at 07:30 ET on their day: fresh (SS 7.3),
    the 20-day change -0.064 pp gives "steady" at the 0.25 pp defaults, and the October CPI event
    (prior only, `data_month` 2026-10) takes the provisional receipt's forecast 2.71 and Cleveland
    2.58 (no street consensus yet)."""
    from scout.macro import forecast_lean, inflation_regime

    c = parse_release_calendar(macro_body("release_calendar_live_layout_SYNTHETIC.json"),
                               fomc_time=cfg.fomc_decision_time_et)
    r = rt()
    fc = parse_forecasts(macro_body("forecasts_index_SYNTHETIC.json"))
    at = datetime(2026, 11, 2, 12, 30, tzinfo=UTC)
    assert freshness(c, r, at, cfg) == ("ok", [])
    assert inflation_regime(r, cfg) == "steady"
    assert [f.event_at for f in c.fomc] == [datetime(2026, 12, 9, 19, 0, tzinfo=UTC)]
    lean = forecast_lean(c, fc)
    assert [(x["kind"], x["print_month"]) for x in lean] == [("cpi", "2026-10")]
    assert lean[0]["vs_prior"] == -0.19 and lean[0]["vs_street"] is None and lean[0]["vs_cleveland"] == 0.13
    assert lean[0]["graded_prints"] == 3 and lean[0]["receipt_status"] == "provisional"


def test_is_release_day() -> None:
    events = cal().events
    assert is_release_day(events, date(2026, 11, 6)) and is_release_day(events, date(2026, 11, 12))
    assert not is_release_day(events, date(2026, 11, 2)) and not is_release_day((), date(2026, 11, 6))


# --------------------------------------------------------------------------- reader


def _reader(cfg, clock, results):
    fetcher = FakeFetcher(results)
    raw, health, board = FakeRawSink(), FakeHealthSink(), FakeBoard()
    return NowflationReader(cfg, fetcher, raw, health, board, clock), fetcher, raw, health, board


def test_read_ok(cfg, clock) -> None:
    reader, fetcher, raw, health, board = _reader(cfg, clock, nowflation_results())
    rec = reader.read(NOW)
    assert rec.source == "nowflation" and rec.status == "ok" and rec.reasons == ()
    assert rec.inflation_regime == "steady"  # change_20d -0.064, inside +-0.25
    assert rec.attribution == ATTRIBUTION == "Nowflation https://nowflation.com"
    assert rec.source_generated_at == "2026-11-02T11:05:00Z" and rec.sha256  # the older file (calendar)
    assert [e["kind"] for e in rec.events] == ["nfp", "cpi", "pce", "fomc"]
    assert rec.events[3]["event_at"] == "2026-12-09T19:00:00Z"
    assert rec.realtime["change_20d"] == -0.064
    lean = {x["kind"]: x for x in rec.forecast_lean}
    assert lean["cpi"]["vs_prior"] == -0.1 and lean["cpi"]["vs_street"] is None
    assert lean["cpi"]["graded_prints"] == 3 and lean["cpi"]["note"] == "informational, untested"
    assert [u for _s, u in fetcher.calls] == [CAL_URL, RT_URL, FC_URL]
    assert board.sources["nowflation"].status == "ok"
    assert {h.result for h in health.records} == {"ok"} and len(raw.saved) == 3
    rec.to_record()  # a valid record


def test_forecasts_read_once_a_day(cfg, clock) -> None:
    reader, fetcher, *_ = _reader(cfg, clock, nowflation_results())
    reader.read(NOW)
    reader.read(NOW + timedelta(hours=1))
    assert [u for _s, u in fetcher.calls].count(FC_URL) == 1
    reader.read(NOW + timedelta(days=1))
    assert [u for _s, u in fetcher.calls].count(FC_URL) == 2


def test_daily_request_cap(scout_dir, clock) -> None:
    capped = make_config(scout_dir, host_max_requests_per_day={"nowflation.com": 4})
    reader, fetcher, *_ = _reader(capped, clock, nowflation_results())
    assert reader.read(NOW) is not None  # 3 requests
    assert reader.read(NOW + timedelta(hours=1)) is None  # 1 left, a read needs 2
    assert len(fetcher.calls) == 3
    assert reader.read(NOW + timedelta(days=1)) is not None  # a new ET day


def test_default_cap_is_ten_a_day(cfg, clock) -> None:
    reader, fetcher, *_ = _reader(cfg, clock, nowflation_results())
    for i in range(10):
        reader.read(NOW + timedelta(minutes=i))
    assert len(fetcher.calls) <= 10


def test_calendar_failure_falls_back_to_ics(cfg, clock) -> None:
    results = nowflation_results()
    results[CAL_URL] = fetch_result("nowflation", CAL_URL, None, result="http_error", status=500)
    reader, fetcher, _raw, health, board = _reader(cfg, clock, results)
    rec = reader.read(NOW)
    assert rec.status == "unavailable" and rec.inflation_regime == "unknown"
    assert "release_calendar.json:fetch:http_error" in rec.reasons and "calendar_from_ics" in rec.reasons
    assert [e["kind"] for e in rec.events] == ["nfp", "cpi", "fomc"]
    assert all(e["extra"].get("from_ics") for e in rec.events)
    assert ICS_URL in [u for _s, u in fetcher.calls]
    assert board.sources["nowflation"].status == "unavailable"


def test_calendar_drift_refuses_the_json_and_keeps_no_rows_from_it(cfg, clock) -> None:
    results = nowflation_results(cal="release_calendar_drift_SYNTHETIC.json")
    reader, _f, _raw, health, _board = _reader(cfg, clock, results)
    rec = reader.read(NOW)
    assert rec.status == "unavailable" and "release_calendar.json:schema" in rec.reasons
    assert all(e["extra"].get("from_ics") for e in rec.events)  # nothing from the drifted file
    assert "schema" in {h.result for h in health.records}


def test_drift_without_ics_has_no_events(cfg, clock) -> None:
    results = nowflation_results(cal="release_calendar_drift_SYNTHETIC.json", ics=None)
    rec = _reader(cfg, clock, results)[0].read(NOW)
    assert rec.status == "unavailable" and rec.events == ()


@pytest.mark.parametrize("name", ["realtime_run_not_ok_SYNTHETIC.json", "realtime_not_live_SYNTHETIC.json",
                                  "realtime_last_good_held_SYNTHETIC.json", "realtime_stale_weight_SYNTHETIC.json",
                                  "realtime_all_stale_SYNTHETIC.json"])
def test_stale_realtime_gives_unknown_regime(cfg, clock, name) -> None:
    rec = _reader(cfg, clock, nowflation_results(rt=name))[0].read(NOW)
    assert rec.status == "stale" and rec.inflation_regime == "unknown"
    assert len(rec.events) == 4  # events are still recorded


def test_realtime_drift_is_unavailable(cfg, clock) -> None:
    rec = _reader(cfg, clock, nowflation_results(rt="realtime_drift_SYNTHETIC.json"))[0].read(NOW)
    assert rec.status == "unavailable" and "realtime.json:schema" in rec.reasons and rec.realtime is None


def test_forecasts_drift_is_informational_only(cfg, clock) -> None:
    rec = _reader(cfg, clock, nowflation_results(fc="forecasts_index_drift_SYNTHETIC.json"))[0].read(NOW)
    assert rec.status == "ok" and "informational:forecasts_index.json:schema" in rec.reasons


def test_no_http_is_unavailable(cfg, clock) -> None:
    results = {u: fetch_result("nowflation", u, None, result="no_http", status=None)
               for u in (CAL_URL, RT_URL, FC_URL, ICS_URL)}
    rec = _reader(cfg, clock, results)[0].read(NOW)
    assert rec.status == "unavailable" and rec.events == () and rec.inflation_regime == "unknown"


def test_last_events_feed_the_release_day_check(cfg, clock) -> None:
    reader = _reader(cfg, clock, nowflation_results())[0]
    reader.read(NOW)
    assert is_release_day(reader.last_events, date(2026, 11, 6))


def test_ics_fallback_names_nfp_by_full_release_name_only() -> None:
    """Regression: any summary with "employment" or "jobs" was NFP, so the ADP report (Wednesday)
    was paired with the BLS Employment Situation (Friday) and NFP moved to Wednesday."""
    from scout.nowflation import ics_kind
    assert ics_kind("ADP National Employment Report") is None
    assert ics_kind("Employment Cost Index") is None
    assert ics_kind("JOLTS Job Openings") is None
    assert ics_kind("Weekly Jobless Claims") is None
    assert ics_kind("Employment Situation (NFP)") == "nfp"
    assert ics_kind("Nonfarm Payrolls") == "nfp"
    assert ics_kind("CPI release") == "cpi" and ics_kind("Consumer Price Index") == "cpi"
    assert ics_kind("FOMC decision") == "fomc" and ics_kind("PCE") == "pce"


def test_cap_counts_requests_sent_before_a_restart(cfg, clock) -> None:
    """Regression: the reader's count lived in memory; it now also reads the fetcher's day count,
    which survives a restart."""
    reader, fetcher, *_ = _reader(cfg, clock, nowflation_results())
    fetcher.requests_today = lambda host, now: 9 if host == "nowflation.com" else 0
    assert reader.requests_left(NOW) == 1
    assert reader.read(NOW) is None and fetcher.calls == []


def test_cap_cannot_go_above_ten(cfg, clock) -> None:
    import copy
    loose = copy.copy(cfg)
    object.__setattr__(loose, "host_max_requests_per_day", {"nowflation.com": 500})  # as if past validation
    reader, *_ = _reader(loose, clock, nowflation_results())
    assert reader.cap == 10


def test_forecasts_saved_today_are_not_asked_again_after_a_restart(cfg, clock, tmp_path) -> None:
    from scout.paths import ScoutPaths
    from scout.raw import RawStore
    raw = RawStore(ScoutPaths.from_config(cfg), cfg)
    raw.save("nowflation", "forecasts_index.json", macro_body("forecasts_index_SYNTHETIC.json"), NOW)
    fetcher = FakeFetcher(nowflation_results())
    reader = NowflationReader(cfg, fetcher, raw, FakeHealthSink(), FakeBoard(), clock)
    rec = reader.read(NOW + timedelta(hours=1))
    assert FC_URL not in [u for _s, u in fetcher.calls] and rec.forecast_lean


def test_an_old_realtime_file_makes_the_record_stale_at_0915(cfg, clock) -> None:
    """Regression: source_generated_at came from the calendar alone, so a realtime.json from
    yesterday (ok at the 08:20 read) stayed fresh until tomorrow 09:15 instead of today 09:15."""
    from scout.macro import stale_after

    rt = json.loads(macro_body("realtime_SYNTHETIC.json"))
    rt["generated_at"] = "2026-11-01T13:05:00Z"  # yesterday 08:05 ET
    results = nowflation_results()
    results[RT_URL] = json.dumps(rt).encode()
    reader, *_ = _reader(cfg, clock, results)
    at = datetime(2026, 11, 2, 13, 20, tzinfo=UTC)  # 08:20 ET
    clock.set(at)
    rec = reader.read(at)
    assert rec.status == "ok" and rec.source_generated_at == "2026-11-01T13:05:00Z"
    assert stale_after(rec, cfg) == "2026-11-02T14:15:00Z"  # today 09:15 ET
