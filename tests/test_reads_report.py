"""The brief's thesis-reads section: counts by reader and the reads spend, apart from the other jobs."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from conftest import make_config
from helpers_fake_anthropic import make_ctx
from scout.brief import build_brief
from scout.clock import ManualClock
from scout.llm_records import write_call
from scout.models import LlmCall
from scout.paths import ScoutPaths
from scout.reads_budget import reads_ledger_paths
from scout.reads_file import ReadsWriter

DAY = date(2026, 11, 3)  # a Tuesday; the previous trading day is 2026-11-02
NOW = datetime(2026, 11, 3, 13, 20, tzinfo=timezone.utc)


def _call(paths: ScoutPaths, job: str, cost: float, at: str) -> None:
    write_call(paths, LlmCall(job=job, at=at, model_id="m", effort="low", prompt_sha256="x", schema_version=1,
                              request_id=None, usage={}, cost_usd=cost, estimate_usd=cost, stop_reason="end_turn",
                              validation="ok", retry=False, fact_ids=(), symbols=()))


def test_section_counts_reads_and_spend(scout_dir: Path) -> None:
    cfg = make_config(scout_dir, llm_enabled=True, reads_enabled=True)
    paths = ScoutPaths.from_config(cfg)
    w = ReadsWriter(paths, ManualClock(datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)))
    base = {"format": 1, "trading_day": "2026-11-02", "symbol": "ABCD"}
    for strength in (5, 4, 2):
        w.append({**base, "kind": "first", "reader": "R1", "strength": strength}, number=True)
    w.append({**base, "kind": "reread", "reader": "R1", "strength": 4}, number=True)
    w.append({**base, "kind": "first", "reader": "R2", "strength": 3}, number=True)
    w.append({**base, "kind": "owner", "reader": "owner", "strength": 4}, number=True)
    for why in ("stale", "stale", "cap"):
        w.append({**base, "kind": "skip", "reader": "R1", "why": why}, number=False)
    _call(reads_ledger_paths(paths), "READ:R1", 0.25, "2026-11-02T15:00:00Z")  # where execute writes reads calls
    _call(reads_ledger_paths(paths), "READ:R2", 0.10, "2026-11-02T15:00:00Z")
    _call(paths, "J1", 0.40, "2026-11-02T15:00:00Z")
    text = build_brief(make_ctx(cfg, paths, ManualClock(NOW)), DAY, NOW)
    section = text[text.index("## Thesis reads"):]
    section = section[: section.index("\n## ")]
    assert ("R1: first 3 (strong 2), rereads 1, skips cap 1, stale 2; R2: first 1 (strong 0), rereads 0, skips none; "
            "owner 1 (strong 1)") in section
    assert "Reads spend $0.3500 (R1 $0.2500, R2 $0.1000) of $1.50 a day." in section
    assert "2026-11-03 so far: no reads." in section
    assert "this month $0.3500 of $20.00" in section
    assert "0.40" not in section  # J1's spend is shown in the Claude API section, not here
