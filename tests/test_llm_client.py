"""The transport, normalising, the shared call flow and the LLM jobs list (SCOUT_SPEC 8.1, 2.8)."""

from __future__ import annotations

import logging
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from conftest import make_config
from helpers_fake_anthropic import (
    FakeAnthropic, fact, fetch_result, j1_reply, label, make_ctx, message, search_result, server_tool_use, text,
    usage,
)
from scout.clock import ManualClock
from scout.config import ScoutConfig
from scout.jobs_llm import build_jobs
from scout.llm_client import LlmApiError, build_transport, call, execute
from scout.llm_ledger import Ledger
from scout.llm_prompts import build_j1
from scout.llm_records import calls_on
from scout.paths import ScoutPaths

T = "2026-11-02T13:00:00Z"
KEY = "sk-ant-api03-SECRETSECRETSECRET123"


@pytest.fixture
def llm_cfg(scout_dir: Path) -> ScoutConfig:
    return make_config(scout_dir, llm_enabled=True)


@pytest.fixture
def lpaths(llm_cfg: ScoutConfig) -> ScoutPaths:
    return ScoutPaths.from_config(llm_cfg)


class _FakeSdk:
    created: list[dict[str, Any]] = []

    class Anthropic:
        def __init__(self, **kw: Any) -> None:
            _FakeSdk.created.append(kw)
            self.messages = types.SimpleNamespace(create=lambda **p: message({"ok": 1}))


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSdk]:
    _FakeSdk.created = []
    mod = types.ModuleType("anthropic")
    mod.Anthropic = _FakeSdk.Anthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return _FakeSdk


# --------------------------------------------------------------------------- build_transport


def test_no_transport_when_off_or_no_key(cfg: ScoutConfig, llm_cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock,
                                         monkeypatch: pytest.MonkeyPatch, fake_sdk: type[_FakeSdk]) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    assert build_transport(make_ctx(cfg, paths, clock)) is None  # llm_enabled = false
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert build_transport(make_ctx(llm_cfg, paths, clock)) is None  # no key
    assert fake_sdk.created == []


def test_transport_settings_and_key_never_logged(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock,
                                                 monkeypatch: pytest.MonkeyPatch, fake_sdk: type[_FakeSdk],
                                                 caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")
    caplog.set_level(logging.DEBUG)
    tr = build_transport(make_ctx(llm_cfg, lpaths, clock))
    assert tr is not None
    kw = fake_sdk.created[0]
    assert kw["max_retries"] == 0 and kw["timeout"] == llm_cfg.llm_timeout_s and kw["api_key"] == KEY
    assert "ANTHROPIC_LOG" not in os.environ
    assert KEY not in repr(tr) and KEY not in str(vars(tr).keys())
    assert KEY not in caplog.text and KEY not in "".join(capsys.readouterr())


def test_key_never_written_to_files(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    req = build_j1([fact("ABC", "ABC wins", T)], llm_cfg)
    tr = FakeAnthropic(RuntimeError(f"401 bad key {KEY}"), j1_reply(("F1", label("ABC", ["F1"]))))
    execute(tr, req, Ledger(lpaths, llm_cfg, clock), lpaths, clock)
    for p in lpaths.root.rglob("*"):
        if p.is_file():
            assert KEY not in p.read_text(encoding="utf-8", errors="ignore")


# --------------------------------------------------------------------------- call


def test_call_normalises_message(llm_cfg: ScoutConfig) -> None:
    msg = message([server_tool_use("web_search", "t1", query="XYZ news"),
                   search_result("t1", ["https://www.sec.gov/a"]),
                   fetch_result("t2", "https://www.sec.gov/a", "page text"), text({"a": 1})],
                  use=usage(10, 20, 30, 40, searches=2, fetches=1), request_id="req_42", stop_reason="end_turn")
    resp = call(FakeAnthropic(msg), build_j1([fact("ABC", "t", T)], llm_cfg))
    assert resp.request_id == "req_42" and resp.stop_reason == "end_turn" and resp.model == "claude-haiku-5-5"
    assert resp.usage == {"input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 30,
                          "cache_read_input_tokens": 40, "web_search_requests": 2, "web_fetch_requests": 1}
    assert [b["type"] for b in resp.blocks] == ["server_tool_use", "web_search_tool_result", "web_fetch_tool_result",
                                                "text"]
    assert resp.blocks[2]["content"]["content"]["source"]["data"] == "page text"
    assert resp.text == '{"a": 1}' and len(resp.result_blocks()) == 2


def test_call_error_carries_type_only(llm_cfg: ScoutConfig) -> None:
    with pytest.raises(LlmApiError) as err:
        call(FakeAnthropic(ConnectionError(f"x-api-key: {KEY}")), build_j1([fact("ABC", "t", T)], llm_cfg))
    assert str(err.value) == "ConnectionError" and KEY not in repr(err.value)


# --------------------------------------------------------------------------- execute


def _run(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock, *replies: Any) -> tuple[Any, FakeAnthropic]:
    tr = FakeAnthropic(*replies)
    req = build_j1([fact("ABC", "ABC wins", T)], llm_cfg)
    return execute(tr, req, Ledger(lpaths, llm_cfg, clock), lpaths, clock), tr


def test_execute_ok(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock) -> None:
    out, tr = _run(llm_cfg, lpaths, clock, j1_reply(("F1", label("ABC", ["F1"]))))
    assert out.ok and out.calls == 1 and len(tr.calls) == 1 and out.request_id == "req_fake_1"
    rec, = calls_on(lpaths, clock.now().date())
    assert rec["validation"] == "ok" and rec["retry"] is False and rec["job"] == "J1"
    assert rec["cost_usd"] > 0 and rec["estimate_usd"] >= rec["cost_usd"] and rec["fact_ids"] and rec["symbols"] == ["ABC"]
    assert rec["prompt_sha256"] and rec["schema_version"] == 1 and rec["model_id"] == "claude-haiku-5-5"


@pytest.mark.parametrize("first, validation", [
    (message({"labels": []}, stop_reason="refusal"), "refusal"),
    (message({"labels": []}, stop_reason="max_tokens"), "max_tokens"),
    (message("not json at all"), "schema_fail"),
    (message({"labels": [], "extra": 1}), "schema_fail"),
])
def test_one_retry_then_ok(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock, first: Any,
                           validation: str) -> None:
    out, tr = _run(llm_cfg, lpaths, clock, first, j1_reply(("F1", label("ABC", ["F1"]))))
    assert out.ok and len(tr.calls) == 2
    recs = calls_on(lpaths, clock.now().date())
    assert [(r["validation"], r["retry"]) for r in recs] == [(validation, False), ("ok", True)]


def test_two_failures_unlabelled(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock) -> None:
    out, tr = _run(llm_cfg, lpaths, clock, message("{}", stop_reason="refusal"), message({"bad": 1}),
                   j1_reply(("F1", label("ABC", ["F1"]))))
    assert out.status == "unlabelled" and len(tr.calls) == 2 and tr.replies  # never a third call
    assert [r["validation"] for r in calls_on(lpaths, clock.now().date())] == ["refusal", "schema_fail"]


def test_api_error_not_retried(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock) -> None:
    out, tr = _run(llm_cfg, lpaths, clock, TimeoutError("slow"), j1_reply(("F1", label("ABC", ["F1"]))))
    assert out.status == "unlabelled" and out.validation == "api_error" and len(tr.calls) == 1
    assert calls_on(lpaths, clock.now().date())[0]["validation"] == "api_error"


def test_no_transport_records_no_key(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock) -> None:
    req = build_j1([fact("ABC", "t", T)], llm_cfg)
    out = execute(None, req, Ledger(lpaths, llm_cfg, clock), lpaths, clock)
    assert out.status == "refused" and out.reason == "no_key"
    assert calls_on(lpaths, clock.now().date())[0]["validation"] == "no_key"


def test_disabled_writes_nothing(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock) -> None:
    tr = FakeAnthropic()
    out = execute(tr, build_j1([fact("ABC", "t", T)], cfg), Ledger(paths, cfg, clock), paths, clock)
    assert out.status == "refused" and out.reason == "llm_disabled" and tr.calls == []
    assert calls_on(paths, clock.now().date()) == []


# --------------------------------------------------------------------------- jobs_llm


def test_build_jobs(cfg: ScoutConfig, llm_cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock) -> None:
    assert build_jobs(make_ctx(cfg, paths, clock, FakeAnthropic())) == []  # llm off
    ctx = make_ctx(llm_cfg, paths, clock, None)
    assert build_jobs(ctx) == []  # on, but no key -> no transport
    names = [j.name for j in build_jobs(make_ctx(llm_cfg, paths, clock, FakeAnthropic()))]
    assert names == ["llm_j1", "llm_j3", "llm_j3_gap"]


def test_build_jobs_without_key_uses_real_builder(llm_cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock) -> None:
    ctx = make_ctx(llm_cfg, paths, clock)
    del ctx._services["llm_transport"]  # AppContext imports scout.llm_client.build_transport
    assert build_jobs(ctx) == []


class APITimeoutError(Exception):
    """Named like the SDK's timeout (the client only ever sees the type name)."""


class APIConnectionError(Exception):
    pass


class BadRequestError(Exception):
    pass


@pytest.mark.parametrize("exc, charged", [(APITimeoutError("t"), True), (APIConnectionError("c"), True),
                                          (TimeoutError("t"), True), (BadRequestError("400"), False)])
def test_a_timeout_is_charged_its_estimate(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock, exc: Exception,
                                          charged: bool) -> None:
    """Regression: a timed-out call was recorded at $0, but the server may finish and bill it, so
    the daily and monthly caps never saw that spend."""
    out, _tr = _run(llm_cfg, lpaths, clock, exc)
    rec, = calls_on(lpaths, clock.now().date())
    assert rec["validation"] == "api_error"
    ledger = Ledger(lpaths, llm_cfg, clock)
    if charged:
        assert rec["cost_usd"] == rec["estimate_usd"] > 0 and rec["reason"].startswith("charged_estimate:")
        assert ledger.spent_today() == pytest.approx(rec["estimate_usd"]) and out.cost_usd == pytest.approx(rec["cost_usd"])
    else:
        assert rec["cost_usd"] == 0 and ledger.spent_today() == 0


def test_a_timed_out_j3_call_counts_its_searches(llm_cfg: ScoutConfig, lpaths: ScoutPaths,
                                                 clock: ManualClock) -> None:
    """Regression: a maybe-billed api_error was recorded with all-zero usage, so the server-side
    searches of a timed-out J3 call counted 0 against web_search_max_per_day."""
    from scout.llm_prompts import build_j3

    req = build_j3("ABC", None, [], llm_cfg)
    ledger = Ledger(lpaths, llm_cfg, clock)
    execute(FakeAnthropic(TimeoutError("t")), req, ledger, lpaths, clock)
    rec, = calls_on(lpaths, clock.now().date())
    assert rec["validation"] == "api_error" and rec["usage"]["web_search_requests"] == req.web_search_uses()
    assert ledger.searches_today() == req.web_search_uses()
    execute(FakeAnthropic(LlmApiError("BadRequestError")), req, ledger, lpaths, clock)  # not billed: no searches
    assert ledger.searches_today() == req.web_search_uses()


def test_a_call_whose_record_cannot_be_written_still_counts(scout_dir: Path, clock: ManualClock,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the call record was written only after the billed call, so with a full disk every
    call went out and none was counted: the cost and call caps never triggered. A reservation is now
    written first; when the final record fails it stands at the worst case."""
    import scout.llm_records as records

    real = records.append_jsonl

    def full_disk_after_reservation(paths: Any, target: Any, rec: dict) -> None:
        if rec.get("validation") != "reserved":
            raise OSError(28, "No space left on device")
        real(paths, target, rec)

    monkeypatch.setattr(records, "append_jsonl", full_disk_after_reservation)
    llm_cfg = make_config(scout_dir, llm_enabled=True, llm_daily_cost_cap_usd=0.01)
    lpaths = ScoutPaths.from_config(llm_cfg)
    ledger = Ledger(lpaths, llm_cfg, clock)
    req = build_j1([fact("ABC", "ABC wins", T)], llm_cfg)
    sent = 0
    for _ in range(50):
        tr = FakeAnthropic(*[j1_reply(("F1", label("ABC", ["F1"])))] * 2)
        out = execute(tr, req, ledger, lpaths, clock)
        sent += len(tr.calls)
        if out.status == "refused":
            break
    assert ledger.calls_today("J1") == sent and ledger.spent_today() == pytest.approx(sent * ledger.estimate(req))
    assert ledger.spent_today() <= llm_cfg.llm_daily_cost_cap_usd
    assert sent < 50  # the daily cap stopped the calls


def test_no_reservation_no_call(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    import scout.llm_records as records

    def full_disk(*_a: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(records, "append_jsonl", full_disk)
    out, tr = _run(llm_cfg, lpaths, clock, j1_reply(("F1", label("ABC", ["F1"]))))
    assert out.status == "refused" and out.reason == "ledger_unwritable" and tr.calls == []


def test_an_unreadable_ledger_refuses_every_call(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: an OSError opening the day file read as "nothing spent today"."""
    import builtins

    import scout.llm_ledger as L

    out, _tr = _run(llm_cfg, lpaths, clock, j1_reply(("F1", label("ABC", ["F1"]))))
    assert out.ok
    day_file = str(lpaths.day_file(lpaths.llm, clock.now().date()))
    real_open = builtins.open

    def locked(file: Any, *a: Any, **kw: Any) -> Any:
        if str(file) == day_file:
            raise PermissionError(13, "sharing violation")
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", locked)
    ledger = L.Ledger(lpaths, llm_cfg, clock)
    req = build_j1([fact("ABC", "ABC wins", T)], llm_cfg)
    assert ledger.allow("J1", req) == (False, "ledger_unreadable")
    assert ledger.cap_reached() is True and ledger.status()["cap_reached"] is True
