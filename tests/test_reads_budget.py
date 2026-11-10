"""The thesis reads' own budget (READS_TEST.md 10): independent of the other Claude jobs both ways."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_config
from scout.clock import ManualClock, iso
from scout.config import ScoutConfig
from scout.llm_ledger import Ledger
from scout.llm_prompts import LlmRequest
from scout.llm_records import write_call
from scout.models import LlmCall
from scout.paths import ScoutPaths
from scout.reads_budget import ReadsBudget, reader_job, reads_ledger_paths


def _cfg(scout_dir: Path, **over: object) -> ScoutConfig:
    return make_config(scout_dir, llm_enabled=True, reads_enabled=True, **over)


def _req(job: str, model: str = "claude-haiku-5-5", max_tokens: int = 1000) -> LlmRequest:
    return LlmRequest(job=job, model=model, effort="low", max_tokens=max_tokens, system="s" * 350, user="u" * 350,
                      schema={"type": "object"}, nonce="n")


def _spend(paths: ScoutPaths, clock: ManualClock, job: str, cost: float, *, validation: str = "ok",
           reason: str | None = None) -> None:
    """A ledger record where `execute` puts it: reads calls in state/reads/ledger, the others in state/llm."""
    where = reads_ledger_paths(paths) if job.startswith("READ:") else paths
    write_call(where, LlmCall(job=job, at=iso(clock.now()), model_id="claude-haiku-5-5", effort="low",
                              prompt_sha256="x", schema_version=1, request_id=None, usage={}, cost_usd=cost,
                              estimate_usd=cost, stop_reason="end_turn", validation=validation, retry=False,
                              fact_ids=(), symbols=()), reason=reason)


def _ledger_view(led: Ledger) -> tuple:
    return (led.spent_today(), led.spent_month(), led.calls_today("J1"), led.searches_today(), led.cap_reached(),
            led.cap_reached("J1"), led.allow("J1", _req("J1")), led.status())


def test_no_reads_records_leave_every_ledger_result_unchanged(scout_dir: Path, clock: ManualClock) -> None:
    cfg = _cfg(scout_dir)
    paths = ScoutPaths.from_config(cfg)
    _spend(paths, clock, "J1", 0.40)
    _spend(paths, clock, "J3", 0.10, validation="refused_by_cap", reason="j3_cap")
    led = Ledger(paths, cfg, clock)
    before = _ledger_view(led)
    assert before[0] == pytest.approx(0.50) and before[2] == 1
    # reads records added: the main ledger sees exactly what it saw
    _spend(paths, clock, reader_job("R1"), 0.90)
    _spend(paths, clock, reader_job("R1"), 0.0, validation="refused_by_cap", reason="reads_daily_cap")
    assert _ledger_view(led) == before


def test_reads_spend_never_counts_toward_the_llm_caps(scout_dir: Path, clock: ManualClock) -> None:
    cfg = _cfg(scout_dir, llm_daily_cost_cap_usd=0.50, reads_daily_cost_cap_usd=5.0, reads_monthly_cost_cap_usd=20.0)
    paths = ScoutPaths.from_config(cfg)
    _spend(paths, clock, reader_job("R1"), 3.0)  # far above the llm cap
    _spend(paths, clock, reader_job("R2"), 0.0, validation="refused_by_cap", reason="reader_daily_cap")
    _spend(paths, clock, reader_job("R1"), 0.0, validation="refused_by_cap", reason="reads_daily_cap")
    led = Ledger(paths, cfg, clock)
    assert led.spent_today() == 0.0 and led.spent_month() == 0.0
    for job in (None, "J1", "J3", "J4"):
        assert led.cap_reached(job) is False
    assert led.allow("J1", _req("J1")) == (True, "ok")


def test_other_jobs_spend_and_caps_never_stop_a_read(scout_dir: Path, clock: ManualClock) -> None:
    cfg = _cfg(scout_dir, llm_daily_cost_cap_usd=0.50)
    paths = ScoutPaths.from_config(cfg)
    _spend(paths, clock, "J1", 0.60)
    _spend(paths, clock, "J3", 0.0, validation="refused_by_cap", reason="daily_cap")
    assert Ledger(paths, cfg, clock).cap_reached() is True
    budget = ReadsBudget(paths, cfg, clock)
    assert budget.spent_today() == 0.0 and budget.cap_reached(reader_job("R1")) is False
    assert budget.allow(reader_job("R1"), _req(reader_job("R1"))) == (True, "ok")


def test_reads_daily_cap(scout_dir: Path, clock: ManualClock) -> None:
    cfg = _cfg(scout_dir, reads_daily_cost_cap_usd=1.50)
    paths = ScoutPaths.from_config(cfg)
    budget = ReadsBudget(paths, cfg, clock)
    _spend(paths, clock, reader_job("R1"), 1.4999)
    assert budget.allow(reader_job("R1"), _req(reader_job("R1"))) == (False, "reads_daily_cap")
    assert budget.cap_reached() is False  # not yet met: a cheaper call could still fit
    _spend(paths, clock, reader_job("R1"), 0.0, validation="refused_by_cap", reason="reads_daily_cap")
    assert budget.cap_reached() is True
    clock.advance(86400)  # the next ET day: the day cap starts again
    assert budget.allow(reader_job("R1"), _req(reader_job("R1"))) == (True, "ok")


def test_reads_monthly_cap(scout_dir: Path, clock: ManualClock) -> None:
    cfg = _cfg(scout_dir, reads_monthly_cost_cap_usd=2.0)
    paths = ScoutPaths.from_config(cfg)
    budget = ReadsBudget(paths, cfg, clock)
    _spend(paths, clock, reader_job("R1"), 1.0)
    clock.advance(86400)
    _spend(paths, clock, reader_job("R2"), 1.0)
    clock.advance(86400)
    assert budget.spent_today() == 0.0 and budget.spent_month() == pytest.approx(2.0)
    assert budget.allow(reader_job("R1"), _req(reader_job("R1"))) == (False, "reads_monthly_cap")
    assert budget.cap_reached() is True


def test_reader_own_cap_stops_only_that_reader(scout_dir: Path, clock: ManualClock) -> None:
    cfg = _cfg(scout_dir)  # R2 has daily_cost_cap_usd 0.75 (owner, 2026-11-11)
    paths = ScoutPaths.from_config(cfg)
    budget = ReadsBudget(paths, cfg, clock)
    _spend(paths, clock, reader_job("R2"), 0.75)
    r2 = _req(reader_job("R2"), model="claude-opus-5-5")
    assert budget.allow(reader_job("R2"), r2) == (False, "reader_daily_cap")
    assert budget.cap_reached(reader_job("R2")) is True
    assert budget.cap_reached(reader_job("R1")) is False and budget.cap_reached() is False
    assert budget.allow(reader_job("R1"), _req(reader_job("R1"))) == (True, "ok")


def test_reads_off_or_unknown_reader_is_refused_without_a_cap(scout_dir: Path, clock: ManualClock) -> None:
    cfg = make_config(scout_dir, llm_enabled=True)  # reads_enabled stays false
    paths = ScoutPaths.from_config(cfg)
    assert ReadsBudget(paths, cfg, clock).allow(reader_job("R1"), _req(reader_job("R1"))) == (False, "llm_disabled")
    cfg = _cfg(scout_dir)
    assert ReadsBudget(paths, cfg, clock).allow(reader_job("R9"), _req(reader_job("R9"))) == (False, "job_disabled")


def test_budget_status_by_reader(scout_dir: Path, clock: ManualClock) -> None:
    cfg = _cfg(scout_dir)
    paths = ScoutPaths.from_config(cfg)
    _spend(paths, clock, reader_job("R1"), 0.25)
    _spend(paths, clock, reader_job("R2"), 0.10)
    _spend(paths, clock, "J1", 0.33)
    st = ReadsBudget(paths, cfg, clock).reads_status()
    assert st["spent_today_usd"] == pytest.approx(0.35)
    assert st["by_reader_today_usd"] == {"R1": 0.25, "R2": 0.10}


def test_a_reader_month_cap_stops_only_that_reader(scout_dir: Path, clock: ManualClock) -> None:
    cfg = _cfg(scout_dir)  # R2 has monthly_cost_cap_usd 12.0 (owner, 2026-11-11)
    assert cfg.reads.readers["R2"].monthly_cost_cap_usd == 12.0 and cfg.reads.readers["R1"].monthly_cost_cap_usd == 0
    paths = ScoutPaths.from_config(cfg)
    budget = ReadsBudget(paths, cfg, clock)
    _spend(paths, clock, reader_job("R2"), 11.9999)
    clock.advance(86400)  # a later day of the same month: R2's own day cap is fresh
    r2 = _req(reader_job("R2"), model="claude-opus-5-5")
    assert budget.allow(reader_job("R2"), r2) == (False, "reader_monthly_cap")
    assert budget.cap_reached(reader_job("R2")) is False  # not yet met: a cheaper call could still fit
    _spend(paths, clock, reader_job("R2"), 0.0, validation="refused_by_cap", reason="reader_monthly_cap")
    assert budget.stop_reason(reader_job("R2")) == "reader_monthly_cap"
    assert budget.cap_reached(reader_job("R1")) is False and budget.cap_reached() is False
    assert budget.allow(reader_job("R1"), _req(reader_job("R1"))) == (True, "ok")  # R1 keeps the month's rest


def test_a_reader_month_cap_is_validated(scout_dir: Path) -> None:
    from scout.config_load import ConfigError  # noqa: PLC0415

    with pytest.raises(ConfigError, match="monthly_cost_cap_usd"):
        _cfg(scout_dir, llm={"readers": {"R2": {"monthly_cost_cap_usd": -1.0}}})


def test_an_unreadable_reads_ledger_is_its_own_stop_reason(scout_dir: Path, clock: ManualClock,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    import scout.llm_ledger as L  # noqa: PLC0415

    cfg = _cfg(scout_dir)
    paths = ScoutPaths.from_config(cfg)

    def unreadable(_path: object) -> list:
        raise L.LedgerUnreadable("PermissionError")

    monkeypatch.setattr(L, "_read", unreadable)
    budget = ReadsBudget(paths, cfg, clock)
    assert budget.stop_reason(reader_job("R1")) == "ledger_unreadable" and budget.cap_reached() is True
    assert budget.allow(reader_job("R1"), _req(reader_job("R1"))) == (False, "ledger_unreadable")
