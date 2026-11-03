"""BLS results (jobs, unemployment, CPI): parsing, results, expectations, the reader and the brief."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from conftest import make_config
from helpers_fakes import FakeFetcher
from helpers_macro import BLS_DATA_BASE, bls_data_results, macro_body

from scout.bls_data import (SERIES, BlsDataReader, MonthValue, expected, jobs_trend, parse_series, results_from,
                            summary_lines)
from scout.clock import parse_iso
from scout.macro import SchemaError, daily_body, stale_after

PAY, UN, CPI = SERIES["payrolls"], SERIES["unemployment"], SERIES["cpi"]


def _body(series_id: str) -> bytes:
    return macro_body(f"bls_data_{series_id}_SYNTHETIC.json")


def _edit(series_id: str, change) -> bytes:
    obj = json.loads(_body(series_id))
    change(obj)
    return json.dumps(obj).encode()


class Recorder:
    def __init__(self) -> None:
        self.records, self.sources = [], {}

    def record(self, rec) -> None:
        self.records.append(rec)

    def update(self, name, state) -> None:
        self.sources[name] = state


class Raw:
    def save(self, source, name, body, now) -> str:
        return "sha-" + name


def _reader(cfg, clock, results=None, history=()):
    fetch = FakeFetcher(results if results is not None else bls_data_results())
    rec = Recorder()
    reader = BlsDataReader(cfg, fetch, Raw(), rec, rec, clock, history=lambda: history)
    return reader, rec, fetch


# --------------------------------------------------------------------------- parsing


def test_parse_series_newest_first_with_preliminary_flags() -> None:
    rows = parse_series(_body(PAY), PAY)
    assert rows[0].month == "2026-09" and rows[0].preliminary and rows[1].preliminary and not rows[2].preliminary
    assert [r.month for r in rows] == sorted((r.month for r in rows), reverse=True)


def test_parse_series_leaves_out_the_annual_average_and_keeps_dash_as_missing() -> None:
    cpi = parse_series(_body(CPI), CPI)
    assert all(not r.month.endswith("-13") for r in cpi)
    un = {r.month: r.value for r in parse_series(_body(UN), UN)}
    assert un["2025-10"] is None and un["2026-09"] == 4.3


def test_refused_reply_is_none() -> None:
    assert parse_series(macro_body("bls_data_refused_SYNTHETIC.json"), PAY) is None


@pytest.mark.parametrize("change", [
    lambda o: o.pop("status"),
    lambda o: o["Results"].update(series=[]),
    lambda o: o["Results"]["series"][0].update(seriesID="OTHER"),
    lambda o: o["Results"]["series"][0]["data"][0].update(period="Q03"),
    lambda o: o["Results"]["series"][0]["data"][0].update(value="4.x"),
    lambda o: o["Results"]["series"][0]["data"][0].update(value=4.3),
    lambda o: o["Results"]["series"][0]["data"].append(dict(o["Results"]["series"][0]["data"][0])),
])
def test_drift_is_refused(change) -> None:
    with pytest.raises(SchemaError):
        parse_series(_edit(UN, change), UN)


def test_not_json_is_refused() -> None:
    with pytest.raises(SchemaError):
        parse_series(b"<html>busy</html>", PAY)


# --------------------------------------------------------------------------- results


def _parsed() -> dict:
    return {name: parse_series(_body(sid), sid) for name, sid in SERIES.items()}


def test_results_from_the_synthetic_series(cfg) -> None:
    res = results_from(_parsed(), cfg, lambda kind, month: {})
    assert res["payrolls"] == {"month": "2026-09", "change_k": 20.0, "prior_change_k": 60.0, "preliminary": True,
                               "street_k": None, "vs_street_k": None, "expected_as_of": None}
    assert res["jobs_trend"] == "weak" and res["jobs_trend_avg_k"] == 40
    assert res["unemployment"] == {"month": "2026-09", "rate": 4.3, "prior": 4.2}
    assert res["cpi"]["yoy_pct"] == 3.2 and res["cpi"]["prior_yoy_pct"] == 3.1


def test_results_compare_with_what_was_expected(cfg) -> None:
    exp = {("nfp", "2026-09"): {"street": 65, "as_of": "2026-10-01T12:20:00Z"},
           ("cpi", "2026-09"): {"street": 3.0, "nowflation_forecast": 3.25, "as_of": "2026-10-14T12:20:00Z"}}
    res = results_from(_parsed(), cfg, lambda kind, month: exp.get((kind, month), {}))
    assert res["payrolls"]["street_k"] == 65 and res["payrolls"]["vs_street_k"] == -45
    assert res["cpi"]["vs_street"] == 0.2 and res["cpi"]["vs_nowflation"] == -0.05


@pytest.mark.parametrize("changes, want", [([200, 160, 150], "strong"), ([100, 90, 80], "steady"),
                                           ([50, 40, 60], "weak"), ([100, None, 80], "unknown"), ([100], "unknown")])
def test_jobs_trend(cfg, changes, want) -> None:
    assert jobs_trend(changes, cfg) == want


def test_jobs_trend_follows_config(scout_dir) -> None:
    cfg = make_config(scout_dir, jobs_trend_months=2, jobs_strong_k=50.0, jobs_weak_k=10.0)
    assert jobs_trend([60, 50], cfg) == "strong"


def test_missing_month_gives_no_change(cfg) -> None:
    rows = [MonthValue("2026-09", 100.0, False), MonthValue("2026-07", 90.0, False)]
    res = results_from({"payrolls": rows}, cfg, lambda k, m: {})
    assert res["payrolls"]["change_k"] is None and res["jobs_trend"] == "unknown" and res["jobs_trend_avg_k"] is None


# --------------------------------------------------------------------------- expectations (no look-ahead)


def _nf_line(seen: str, street) -> dict:
    return {"source": "nowflation", "first_seen_at": seen,
            "events": [{"kind": "nfp", "street": street, "nowflation_forecast": None,
                        "extra": {"data_month": "2026-09"}}]}


def test_expected_uses_the_last_line_seen_before_the_read() -> None:
    history = [_nf_line("2026-09-30T12:20:00Z", 60), _nf_line("2026-10-01T12:20:00Z", 65),
               _nf_line("2026-10-02T12:35:00Z", 999), {"source": "bls", "first_seen_at": "2026-10-01T00:00:00Z"}]
    got = expected(history, "nfp", "2026-09", parse_iso("2026-10-02T12:31:00Z"))
    assert got == {"street": 65, "nowflation_forecast": None, "as_of": "2026-10-01T12:20:00Z"}
    assert expected(history, "nfp", "2026-08", parse_iso("2026-10-02T12:31:00Z")) == {}
    assert expected(history, "nfp", "2026-09", parse_iso("2026-09-01T00:00:00Z")) == {}


# --------------------------------------------------------------------------- the reader


def test_read_ok(cfg, clock, et) -> None:
    clock.set(et("2026-11-03", "08:40"))
    history = [_nf_line("2026-10-01T12:20:00Z", 65)]
    reader, rec, fetch = _reader(cfg, clock, history=history)
    out = reader.read(clock.now())
    assert out.status == "ok" and out.source == "bls_data" and out.reasons == ()
    assert out.results["payrolls"]["vs_street_k"] == -45 and out.results["unemployment"]["rate"] == 4.3
    assert [u for _s, u in fetch.calls] == [BLS_DATA_BASE + s for s in SERIES.values()]
    assert rec.sources["bls_data"].status == "ok" and len(rec.records) == 3
    assert out.to_record()["results"] == out.results  # kept in the macro history


def test_read_with_one_series_failing_is_stale(cfg, clock, et) -> None:
    clock.set(et("2026-11-03", "08:40"))
    results = bls_data_results()
    results[BLS_DATA_BASE + CPI] = macro_body("bls_data_refused_SYNTHETIC.json")
    reader, rec, _ = _reader(cfg, clock, results)
    out = reader.read(clock.now())
    assert out.status == "stale" and out.reasons == ("bls_refused",)
    assert "cpi" not in out.results and "payrolls" in out.results


def test_read_with_nothing_is_unavailable(cfg, clock, et) -> None:
    clock.set(et("2026-11-03", "08:40"))
    reader, rec, _ = _reader(cfg, clock, {})
    out = reader.read(clock.now())
    assert out.status == "unavailable" and out.results is None
    assert rec.sources["bls_data"].status == "unavailable"


def test_old_months_are_stale(cfg, clock, et) -> None:
    clock.set(et("2027-01-05", "08:40"))  # September data in January: BLS has stopped updating
    reader, _, _ = _reader(cfg, clock)
    out = reader.read(clock.now())
    assert out.status == "stale" and "old_month:payrolls" in out.reasons


def test_a_bad_history_never_stops_a_read(cfg, clock, et) -> None:
    clock.set(et("2026-11-03", "08:40"))
    fetch = FakeFetcher(bls_data_results())

    def broken():
        raise OSError("disk")

    out = BlsDataReader(cfg, fetch, Raw(), None, None, clock, history=broken).read(clock.now())
    assert out.status == "ok" and out.results["payrolls"]["street_k"] is None


def test_stale_after_and_daily_view(cfg, clock, et) -> None:
    clock.set(et("2026-11-03", "08:40"))
    reader, _, _ = _reader(cfg, clock)
    out = reader.read(clock.now())
    assert parse_iso(stale_after(out, cfg)) == parse_iso(out.fetched_at) + timedelta(days=2)
    line = out.to_record()
    body = daily_body({"bls_data": line}, [], cfg, clock.now())
    assert body["results"] == out.results and body["sources"]["bls_data"]["status"] == "ok"


# --------------------------------------------------------------------------- the brief


def test_summary_lines(cfg) -> None:
    res = results_from(_parsed(), cfg, lambda kind, month: {"street": 65} if kind == "nfp" else
                       {"nowflation_forecast": 3.25})
    lines = summary_lines(res)
    assert lines[0] == ("- Jobs, September 2026: +20k (first estimate) vs +65k expected (a miss, -45k); "
                        "month before +60k")
    assert lines[1] == "- Jobs trend: weak (average +40k a month)"
    assert lines[2] == "- Unemployment, September 2026: 4.3% (before: 4.2%)"
    assert lines[3] == ("- CPI, September 2026: 3.2% a year (before: 3.1%); Nowflation's forecast was 3.25% "
                        "(off by -0.05)")
    assert summary_lines(None) == ["- No BLS results yet."]
