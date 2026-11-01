"""J4: the brief paragraph, display only (SCOUT_SPEC 8.2)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from conftest import make_config
from helpers_fake_anthropic import FakeAnthropic, message
from helpers_fakes import write_records
from scout.clock import ManualClock
from scout.config import ScoutConfig
from scout.llm_j4 import J4Job
from scout.llm_ledger import Ledger
from scout.llm_records import calls_on, labels_on
from scout.paths import ScoutPaths

ITEMS = [{"id": "C1", "ticker": "ABC", "fact_id": "fa", "text": "ABC [contract] wins Navy contract"},
         {"id": "M1", "text": "Tue 03 Nov 08:30 ET: CPI"}]


@pytest.fixture
def llm_cfg(scout_dir: Path) -> ScoutConfig:
    return make_config(scout_dir, llm_enabled=True)


@pytest.fixture
def lpaths(llm_cfg: ScoutConfig) -> ScoutPaths:
    return ScoutPaths.from_config(llm_cfg)


def test_paragraph_escaped_and_watch_list_checked(llm_cfg: ScoutConfig, lpaths: ScoutPaths, clock: ManualClock) -> None:
    out = {"paragraph": "Quiet open. <script>alert(1)</script> **[buy](http://x)**",
           "watch_list": [{"ticker": "ABC", "reason": "contract", "evidence_ids": ["C1"]},
                          {"ticker": "PUMP", "reason": "not in input", "evidence_ids": ["C1"]},
                          {"ticker": "ABC", "reason": "bad evidence", "evidence_ids": ["C9"]}]}
    tr = FakeAnthropic(message(out, model="claude-sonnet-5-5"))
    text = J4Job(llm_cfg, lpaths, tr, Ledger(lpaths, llm_cfg, clock), clock).paragraph(ITEMS, clock.now())
    assert text is not None and "display only" in text
    assert "<script>" not in text and "&lt;script&gt;" in text and "\\*\\*\\[buy\\]" in text
    assert "- ABC: contract (items C1)" in text and "PUMP" not in text and "bad evidence" not in text
    assert "tools" not in tr.calls[0]
    assert calls_on(lpaths, date(2026, 11, 2))[0]["job"] == "J4" and labels_on(lpaths, date(2026, 11, 2)) == []


def test_none_when_off_capped_or_failed(cfg: ScoutConfig, paths: ScoutPaths, llm_cfg: ScoutConfig,
                                        lpaths: ScoutPaths, clock: ManualClock, scout_dir: Path) -> None:
    tr = FakeAnthropic()
    assert J4Job(cfg, paths, tr, Ledger(paths, cfg, clock), clock).paragraph(ITEMS, clock.now()) is None
    job = J4Job(llm_cfg, lpaths, FakeAnthropic(message("x", stop_reason="refusal"), message("x", stop_reason="refusal")),
                Ledger(lpaths, llm_cfg, clock), clock)
    assert job.paragraph(ITEMS, clock.now()) is None
    assert job.paragraph([], clock.now()) is None
    write_records(scout_dir, "llm", [{"job": "J1", "at": "2026-11-02T13:00:00Z", "cost_usd": 5.0,
                                      "validation": "ok", "usage": {}}])
    tr2 = FakeAnthropic()
    assert J4Job(llm_cfg, lpaths, tr2, Ledger(lpaths, llm_cfg, clock), clock).paragraph(ITEMS, clock.now()) is None
    assert tr.calls == [] and tr2.calls == []
