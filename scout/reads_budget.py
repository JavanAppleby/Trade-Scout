"""The thesis reads' own budget (READS_TEST.md 10; owner, 2026-11-11).

Every reads call goes through the shared flow of `scout.llm_client.execute` with the same pricing,
estimates and records as Scout's other Claude jobs: it is reserved at its worst case before it is
sent and priced when the answer comes back, under the job name `READ:<reader id>`. Its records go
to a folder of their own, `state/reads/ledger/<ET date>.jsonl` (`reads_ledger_paths`: Scout's paths
with the ledger folder moved there), so the main ledger in `state/llm/` never holds or parses a
reads record, and this budget never sees another job's. `ReadsBudget` is `scout.llm_ledger.Ledger`
on that folder, with the reads caps. It refuses a call that would cross

- `reads_daily_cost_cap_usd` (reason `reads_daily_cap`) or `reads_monthly_cost_cap_usd`
  (`reads_monthly_cap`), summed over every reader, or
- the reader's own `daily_cost_cap_usd` (`reader_daily_cap`) or `monthly_cost_cap_usd`
  (`reader_monthly_cap`), each when it is above 0, so a costly reader can never use up the main
  test's day or month.

The two budgets are independent both ways: reads spend and refused reads never stop J1, J3 or J4,
and their spend and caps never stop a read. A day file that exists but cannot be read refuses every
read (`ledger_unreadable`), as it does every other job.

Reads run on several worker threads (`scout.reads_service`), so `execute` holds `reserve_lock`
from the cap check to the written reservation: two calls checked at once can never both pass a cap
that only one fits under.
"""

from __future__ import annotations

import threading
from datetime import date
from pathlib import Path
from typing import Any

from scout.llm_ledger import Ledger, LedgerUnreadable
from scout.llm_prompts import LlmRequest
from scout.paths import ScoutPaths

READS_JOB_PREFIX = "READ:"  # the job name of a thesis-read call: "READ:<reader id>"
LEDGER_DIR = "state/reads/ledger"
READS_CAPS = ("reads_daily_cap", "reads_monthly_cap")
READER_CAPS = ("reader_daily_cap", "reader_monthly_cap")
READER_CAP = READER_CAPS[0]
CAP_REASONS = (*READS_CAPS, *READER_CAPS)
UNREADABLE = "ledger_unreadable"

__all__ = ["CAP_REASONS", "LEDGER_DIR", "READER_CAP", "READER_CAPS", "READS_CAPS", "READS_JOB_PREFIX", "UNREADABLE",
           "ReadsBudget", "reader_job", "reads_ledger_paths"]


def reader_job(reader_id: str) -> str:
    return f"{READS_JOB_PREFIX}{reader_id}"


def _reader_id(job: Any) -> str | None:
    return job[len(READS_JOB_PREFIX):] if isinstance(job, str) and job.startswith(READS_JOB_PREFIX) else None


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


class _ReadsLedgerPaths(ScoutPaths):
    """Scout's paths, with the LLM ledger folder moved to the reads' own (`state/reads/ledger`)."""

    @property
    def llm(self) -> Path:
        return self.root / LEDGER_DIR


def reads_ledger_paths(paths: ScoutPaths) -> ScoutPaths:
    """The paths `execute` and `Ledger` use for the reads calls (every write still goes through
    `ScoutPaths.writable`)."""
    if isinstance(paths, _ReadsLedgerPaths):
        return paths
    return _ReadsLedgerPaths(root=paths.root, levels_dir=paths.levels_dir, inbox_dir=paths.inbox_dir,
                             others=paths.others)


class ReadsBudget(Ledger):
    """The ledger of the reads calls alone (same pricing, estimates and records as `Ledger`)."""

    def __init__(self, paths: ScoutPaths, cfg: Any, clock: Any) -> None:
        super().__init__(reads_ledger_paths(paths), cfg, clock)
        self.reserve_lock = threading.Lock()  # held by `execute` from the cap check to the reservation

    def _month(self) -> list[tuple[date, dict[str, Any]]]:
        """(ET day, record) for every settled record of this month; LedgerUnreadable when a file
        exists but cannot be read."""
        today = self._today()
        try:
            files = sorted(self.paths.llm.glob(f"{today:%Y-%m}-*.jsonl"))
        except OSError as exc:
            raise LedgerUnreadable(type(exc).__name__) from None
        days = {today}  # today's file is always read, as `Ledger.spent_today` does
        for path in files:
            try:
                days.add(date.fromisoformat(path.stem))
            except ValueError:
                continue
        return [(day, r) for day in sorted(days) for r in self._records(day)]

    def _spend(self) -> dict[str, float]:
        """today, month, and per reader `<id>:day` / `<id>:month`, in one pass over the month."""
        today = self._today()
        out: dict[str, float] = {"today": 0.0, "month": 0.0}
        for day, r in self._month():
            cost, rid = _num(r.get("cost_usd")), _reader_id(r.get("job")) or "?"
            out["month"] += cost
            out[f"{rid}:month"] = out.get(f"{rid}:month", 0.0) + cost
            if day == today:
                out["today"] += cost
                out[f"{rid}:day"] = out.get(f"{rid}:day", 0.0) + cost
        return out

    def reader_spent_today(self, reader_id: str) -> float:
        return self._spend().get(f"{reader_id}:day", 0.0)

    def reader_spent_month(self, reader_id: str) -> float:
        return self._spend().get(f"{reader_id}:month", 0.0)

    def _allow(self, job: str, request: LlmRequest) -> tuple[bool, str]:
        cfg, reads = self.cfg, self.cfg.reads
        if not cfg.llm_enabled or not reads.enabled:
            return False, "llm_disabled"
        rid = _reader_id(job)
        reader = reads.readers.get(rid) if rid is not None else None
        if reader is None or not reader.enabled:
            return False, "job_disabled"
        if cfg.price(request.model) is None:
            return False, "no_price"
        cost = self.estimate(request)
        spend = self._spend()
        if spend["today"] + cost > reads.daily_cost_cap_usd:
            return False, "reads_daily_cap"
        if spend["month"] + cost > reads.monthly_cost_cap_usd:
            return False, "reads_monthly_cap"
        own_day, own_month = reader.daily_cost_cap_usd, reader.monthly_cost_cap_usd
        if own_day > 0 and spend.get(f"{rid}:day", 0.0) + cost > own_day:
            return False, "reader_daily_cap"
        if own_month > 0 and spend.get(f"{rid}:month", 0.0) + cost > own_month:
            return False, "reader_monthly_cap"
        return True, "ok"

    def stop_reason(self, job: str | None = None) -> str | None:
        """None while reads may go on; else why they stop: a reads cap met or refused today, with
        `job` (`READ:<id>`) also that reader's own caps, or `ledger_unreadable`."""
        try:
            return self._stop_reason(job)
        except LedgerUnreadable:
            return UNREADABLE

    def cap_reached(self, job: str | None = None) -> bool:
        """True once a reads cap was met or refused a read today (with `job`, also that reader's
        own caps). An unreadable ledger counts as reached."""
        return self.stop_reason(job) is not None

    def _stop_reason(self, job: str | None) -> str | None:
        reads = self.cfg.reads
        spend = self._spend()
        if spend["today"] >= reads.daily_cost_cap_usd:
            return "reads_daily_cap"
        if spend["month"] >= reads.monthly_cost_cap_usd:
            return "reads_monthly_cap"
        refused = [r for r in self._records() if r.get("validation") == "refused_by_cap"]
        for reason in READS_CAPS:
            if any(r.get("reason") == reason for r in refused):
                return reason
        rid = _reader_id(job)
        reader = reads.readers.get(rid) if rid is not None else None
        if reader is None:
            return None
        if reader.daily_cost_cap_usd > 0 and spend.get(f"{rid}:day", 0.0) >= reader.daily_cost_cap_usd:
            return "reader_daily_cap"
        if reader.monthly_cost_cap_usd > 0 and spend.get(f"{rid}:month", 0.0) >= reader.monthly_cost_cap_usd:
            return "reader_monthly_cap"
        for reason in READER_CAPS:
            if any(r.get("job") == job and r.get("reason") == reason for r in refused):
                return reason
        if any(r.get("job") == job and r.get("reason") == "reader_monthly_cap" for d, r in self._month()
               if r.get("validation") == "refused_by_cap"):
            return "reader_monthly_cap"
        return None

    def reads_status(self) -> dict[str, Any]:
        """For the brief: today's and this month's reads spend, by reader too."""
        try:
            spend = self._spend()
            by_reader = {k[:-4]: round(v, 6) for k, v in sorted(spend.items()) if k.endswith(":day")}
            return {"spent_today_usd": round(spend["today"], 6), "spent_month_usd": round(spend["month"], 6),
                    "by_reader_today_usd": by_reader, "cap_reached": self.cap_reached()}
        except LedgerUnreadable:
            return {"spent_today_usd": None, "spent_month_usd": None, "by_reader_today_usd": {}, "cap_reached": True,
                    "unreadable": True}
