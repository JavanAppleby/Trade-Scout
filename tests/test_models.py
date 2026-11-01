"""Record shapes (DESIGN 5): every record carries format, never omits a key, and round-trips."""

from __future__ import annotations

import json

import pytest

from scout.models import (
    AnswerRecord, Beat, DueWindowRecord, Fact, Filing, HealthRecord, HeartbeatBody, JasonSnapshot, Label, LevelMeta,
    LlmCall, MacroRecord, PeriodicInfo, SourceState,
)

Z = "2026-11-02T14:00:00Z"

FACT = Fact(fact_id="f" * 64, kind="wire", ticker="ABCD", cik=None, cls="fda", source="prnewswire", feed="main",
            guid="g1", url="https://www.prnewswire.com/x", title="ABCD wins FDA approval", published_at=Z,
            first_seen_at=Z, raw_sha256="0" * 64, exchange="NASDAQ")

SAMPLES = [
    FACT,
    HealthRecord(source="prnewswire", feed="main", kind="wire", enabled=True, gate6=True, start=Z, end=Z, result="ok",
                 http_status=200, items=20, new_items=3, overlap=True, spacing_s=60.0, gap_before=False),
    Beat(at=Z, stopped=False, sources={"prnewswire": "ok"}, gap_open={"prnewswire": False}),
    HeartbeatBody(at=Z, version="0.1.0", stopped=False, sources={}, llm={}, levels={}, calendar_covers_next_45d=True),
    AnswerRecord(symbol="ABCD", as_of=Z, written_at=Z, origin="pilot", fresh_catalyst="true",
                 event_before_next_session="unknown", macro_event_next_session="false", news_headline="true",
                 halt_open="false", reasons={"gate5": ["due_window"]}, evidence={"gate6": ["f1"]}, stale=False),
    DueWindowRecord(ticker="ABCD", cik=1, computed_at=Z, status="ok", last_periodic_form="10-Q",
                    last_periodic_accession="a", report_date="2026-09-30", fiscal_year_end="1231",
                    next_period_end="2026-12-31", next_is_fiscal_year_end=True, due_start="2027-01-10",
                    due_end="2027-04-05", overdue=False, nt_recent=False, nt_accession=None, submissions_fetched_at=Z),
    Label(job="J1", fact_id="f1", status="labelled", ticker="ABCD", catalyst_type="fda", direction="up",
          dilution_risk="low", is_new_information=True, scheduled_event=None, confidence="high", evidence_ids=("f1",),
          summary="s", verified=True, check_failures=(), model_id="m", prompt_sha256="p", schema_version=1,
          request_id="r", usage={}, cost_usd=0.001, labelled_at=Z),
    LlmCall(job="J1", at=Z, model_id="m", effort="low", prompt_sha256="p", schema_version=1, request_id=None, usage={},
            cost_usd=0.0, estimate_usd=0.01, stop_reason=None, validation="refused_by_cap", retry=False),
    MacroRecord(source="nowflation", first_seen_at=Z, fetched_at=Z, status="ok", reasons=(), source_generated_at=Z,
                sha256="s", events=({"kind": "CPI"},), realtime=None, inflation_regime="hot", forecast_lean=(), vix=None,
                attribution="Nowflation"),
    LevelMeta(added_at=Z, sha256="s", original_filename="a.json", session_date="2026-11-03", source_kind="tuesday"),
    JasonSnapshot(first_seen_at=Z, today_et="2026-11-02", files=("a.json",), setups=({"symbol": "SPY"},)),
]


@pytest.mark.parametrize("rec", SAMPLES, ids=lambda r: type(r).__name__)
def test_round_trip_through_json(rec) -> None:  # noqa: ANN001
    d = json.loads(json.dumps(rec.to_record()))
    assert d["format"] == 1
    assert type(rec).from_record(d) == rec


def test_fact_uses_the_class_key() -> None:
    d = FACT.to_record()
    assert d["class"] == "fda" and "cls" not in d
    assert d["items"] == [] and d["halt_code"] is None and d["extra"] == {}  # never omitted


def test_bad_records_raise() -> None:
    d = FACT.to_record()
    with pytest.raises(ValueError, match="format"):
        Fact.from_record({**d, "format": 2})
    with pytest.raises(ValueError, match="title"):
        Fact.from_record({k: v for k, v in d.items() if k != "title"})
    with pytest.raises(ValueError, match="kind"):
        Fact.from_record({**d, "kind": "rumour"})
    with pytest.raises(ValueError):
        Fact.from_record([1, 2])
    assert Fact.from_record({**d, "new_key": 1}) == FACT  # keys are only ever added


def test_non_file_records_have_no_format() -> None:
    assert "format" not in SourceState(enabled=True).to_record()
    f = Filing(form="10-Q", accepted=Z, filing_date="2026-11-02", accession="a", items=("2.02",))
    info = PeriodicInfo(ticker="ABCD", cik=1, status="ok", filings=(f,))
    assert info.to_record()["filings"][0]["items"] == ["2.02"]
    with pytest.raises(ValueError):
        SourceState(enabled=True, status="great")
