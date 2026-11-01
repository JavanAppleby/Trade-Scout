"""The job model (DESIGN 2.1, 8)."""

from __future__ import annotations

import logging
from datetime import time, timedelta

import pytest

from scout.scheduler import DailyJob, EveryJob, OnceAtStartJob, run_due


def test_every_job_busy_and_idle_spacing(cfg, et) -> None:  # noqa: ANN001
    ran = []
    job = EveryJob("edgar_atom", ran.append, 60, idle_interval_s=600, cfg=cfg)
    t0 = et("2026-11-02", "10:00")
    assert job.due(t0)
    job.run(t0)
    assert not job.due(t0 + timedelta(seconds=59)) and job.due(t0 + timedelta(seconds=60))
    night = et("2026-11-02", "21:00")
    job.run(night)
    assert not job.due(night + timedelta(seconds=599)) and job.due(night + timedelta(seconds=600))
    assert ran == [t0, night]


def test_daily_job_trading_days_and_early_close(et) -> None:  # noqa: ANN001
    ran = []
    job = DailyJob("answers", ran.append, time(19, 30), early_close_shift_h=3)
    assert not job.due(et("2026-11-02", "19:29")) and job.due(et("2026-11-02", "19:30"))
    job.run(et("2026-11-02", "19:30"))
    assert not job.due(et("2026-11-02", "22:00"))  # once a day
    assert not job.due(et("2026-11-26", "20:00"))  # Thanksgiving: not a trading day
    assert job.due(et("2026-11-27", "16:30"))  # early close: three hours earlier
    weekend = DailyJob("x", ran.append, time(0, 5), trading_days_only=False)
    assert weekend.due(et("2026-11-01", "00:05"))


def test_once_at_start() -> None:
    job = OnceAtStartJob("tickers", lambda now: None)
    assert job.due(None)  # type: ignore[arg-type]
    job.run(None)  # type: ignore[arg-type]
    assert not job.due(None)  # type: ignore[arg-type]


def test_run_due_isolates_failures_and_redacts(caplog: pytest.LogCaptureFixture, clock) -> None:  # noqa: ANN001
    ran = []

    def bad(now):  # noqa: ANN001
        raise RuntimeError("boom sk-ant-api03-SECRETSECRET for owner@example.com")

    jobs = [OnceAtStartJob("bad", bad), OnceAtStartJob("good", ran.append)]
    with caplog.at_level(logging.ERROR):
        names = run_due(jobs, clock.now(), logging.getLogger("test.scheduler"))
    assert names == ["bad", "good"] and ran == [clock.now()]
    text = caplog.text
    assert "job bad failed: RuntimeError" in text
    assert "sk-ant-" not in text and "owner@example.com" not in text
