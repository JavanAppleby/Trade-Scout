"""J4: the brief paragraph (SCOUT_SPEC 8.2, 10.2; DESIGN 2.6).

`paragraph(brief_inputs, now)` returns Markdown to append to the deterministic brief, or None when
the LLM is off, at a cap, or the reply failed. It is display only: it writes the call record and
nothing else, and the paragraph is escaped like any third-party text. A watch-list entry whose
ticker or evidence ids are not in the input is dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from scout.brief_sections import md_escape
from scout.clock import Clock
from scout.config import ScoutConfig
from scout.llm_checks import check_label
from scout.llm_client import execute
from scout.llm_ledger import Ledger
from scout.llm_prompts import build_j4
from scout.paths import ScoutPaths

__all__ = ["J4Job"]


class J4Job:
    def __init__(self, cfg: ScoutConfig, paths: ScoutPaths, transport: Any, ledger: Ledger, clock: Clock, *,
                 log: logging.Logger | None = None) -> None:
        self.cfg, self.paths, self.transport, self.ledger, self.clock = cfg, paths, transport, ledger, clock
        self.log = log or logging.getLogger("scout.llm_j4")

    def paragraph(self, brief_inputs: Sequence[Mapping[str, Any]], now: datetime) -> str | None:
        if not self.cfg.llm_enabled or not self.cfg.llm_job("J4").enabled or not brief_inputs:
            return None
        if self.ledger.cap_reached("J4"):
            return None
        request = build_j4(brief_inputs, self.cfg)
        outcome = execute(self.transport, request, self.ledger, self.paths, self.clock)
        if not outcome.ok or not isinstance(outcome.obj, dict):
            self.log.info("J4 gave no paragraph: %s", outcome.reason or outcome.validation)
            return None
        tickers = {t for ts in request.item_tickers.values() for t in ts}
        lines = ["## Claude's paragraph (display only, unverified, gates nothing)", "",
                 md_escape(outcome.obj.get("paragraph"), 1200), ""]
        watch = []
        for entry in outcome.obj.get("watch_list", [])[:8]:
            probe = {"ticker": str(entry.get("ticker") or "").upper(), "evidence_ids": entry.get("evidence_ids") or []}
            if check_label(probe, tickers, request.item_ids.keys()) or not probe["evidence_ids"]:
                continue
            ids = ", ".join(md_escape(e, 20) for e in probe["evidence_ids"])
            watch.append(f"- {md_escape(probe['ticker'], 12)}: {md_escape(entry.get('reason'), 200)} (items {ids})")
        if watch:
            lines += ["Watch list:", *watch, ""]
        lines.append(f"_Model {md_escape(request.model, 60)}, cost ${outcome.cost_usd:.4f}._")
        return "\n".join(lines) + "\n"
