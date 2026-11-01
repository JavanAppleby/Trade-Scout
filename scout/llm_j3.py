"""J3 research with server web_search and web_fetch (SCOUT_SPEC 8.2, 8.4; DESIGN 2.6).

Triggers:
- `run_positions(now)`: at `j3_time_et` (19:30 ET) for each Pilot position still open;
- `run_gap_fill(now)`: a Radar find with no Layer-1 wire or filing fact naming it between the prior
  close and `j3_no_fact_after_s` after the find, once that time has passed.
Each symbol is researched at most once per ET day. The label's citations must pass the URL, host,
quote and symbol checks against the same response's result blocks to count as verified. A J3
finding never satisfies gate 6 in v1 (SS 16 Q6) and never changes an answer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from scout.calendar import prior_close_before
from scout.clock import Clock, et_date, iso, parse_iso
from scout.config import ScoutConfig
from scout.inputs import pilot_positions, radar_finds
from scout.llm_checks import check_date, check_hostile, check_j3, check_label, fetched_pages, names_symbol
from scout.llm_client import execute
from scout.llm_ledger import Ledger
from scout.llm_prompts import build_j3, clean_ticker
from scout.llm_records import build_label, labels_on, write_label
from scout.paths import ScoutPaths
from scout.store import read_jsonl

FACT_KINDS = ("wire", "filing")

__all__ = ["J3Job"]


class J3Job:
    def __init__(self, cfg: ScoutConfig, paths: ScoutPaths, transport: Any, ledger: Ledger, clock: Clock, *,
                 log: logging.Logger | None = None, company_for: Any = None) -> None:
        self.cfg, self.paths, self.transport, self.ledger, self.clock = cfg, paths, transport, ledger, clock
        self.log = log or logging.getLogger("scout.llm_j3")
        self.company_for = company_for  # optional callable(symbol) -> company name or None

    # ------------------------------------------------------------------ reading

    def _facts(self, now: datetime) -> list[dict[str, Any]]:
        today = et_date(now)
        out: list[dict[str, Any]] = []
        for day in (today - timedelta(days=1), today):
            out.extend(read_jsonl(self.paths.day_file(self.paths.facts, day)))
        return [f for f in out if f.get("format") == 1 and f.get("kind") in FACT_KINDS]

    def researched_today(self, now: datetime) -> set[str]:
        return {str(r.get("ticker")).upper() for r in labels_on(self.paths, et_date(now))
                if r.get("job") == "J3" and r.get("ticker")}

    def _on(self) -> bool:
        return self.cfg.llm_enabled and self.cfg.llm_job("J3").enabled

    # ------------------------------------------------------------------ triggers

    def run(self, now: datetime) -> int:
        return self.run_gap_fill(now)

    def run_positions(self, now: datetime) -> int:
        if not self._on():
            return 0
        done = self.researched_today(now)
        count = 0
        for symbol in pilot_positions(self.cfg, now):
            if symbol not in done:
                if self.ledger.cap_reached("J3"):
                    break
                count += self.research(symbol, now)
        return count

    def gap_symbols(self, now: datetime) -> list[str]:
        """Radar finds older than `j3_no_fact_after_s` with no Layer-1 fact since the prior close."""
        wait = timedelta(seconds=self.cfg.j3_no_fact_after_s)
        facts = self._facts(now)
        out: list[str] = []
        for find in radar_finds(self.cfg, et_date(now)):
            found = parse_iso(find.found_at)
            symbol = clean_ticker(find.symbol)
            if found is None or symbol is None or now < found + wait or symbol in out:
                continue
            start, end = prior_close_before(found), found + wait
            named = False
            for f in facts:
                seen = parse_iso(f.get("first_seen_at"))
                if (clean_ticker(f.get("ticker")) == symbol and seen is not None and start <= seen <= end):
                    named = True
                    break
            if not named:
                out.append(symbol)
        return out

    def run_gap_fill(self, now: datetime) -> int:
        if not self._on():
            return 0
        done = self.researched_today(now)
        count = 0
        for symbol in self.gap_symbols(now):
            if symbol in done:
                continue
            if self.ledger.cap_reached("J3"):
                break
            count += self.research(symbol, now)
        return count

    # ------------------------------------------------------------------ one symbol

    def research(self, symbol: str, now: datetime) -> int:
        ticker = clean_ticker(symbol)
        if ticker is None:
            return 0
        facts = [f for f in self._facts(now) if clean_ticker(f.get("ticker")) == ticker]
        facts.sort(key=lambda f: str(f.get("first_seen_at")))
        company = self.company_for(ticker) if callable(self.company_for) else None
        request = build_j3(ticker, company, facts[-self.cfg.j1_batch_size:], self.cfg)
        outcome = execute(self.transport, request, self.ledger, self.paths, self.clock)
        if outcome.status == "refused":
            self.log.info("J3 %s not sent: %s", ticker, outcome.reason)
            return 0
        lab: Any = None
        citations: list[dict[str, Any]] = []
        failures: list[str] = []
        if outcome.ok and isinstance(outcome.obj, dict):
            lab = outcome.obj.get("label")
            citations = [c for c in outcome.obj.get("citations", []) if isinstance(c, dict)]
            blocks = list(outcome.response.blocks) if outcome.response else []
            pages = list(fetched_pages(blocks).values())
            item_texts = list(request.item_texts.values())
            failures += check_label(lab, (ticker,), request.item_ids.keys())
            failures += check_j3(lab, citations, blocks, self.cfg.llm_allowed_domains, company=company)
            # a date quote counts only from a page that names this symbol (or company) or an item
            # whose release names this ticker: never from a page about another company
            own_pages = [p for p in pages if names_symbol(p, ticker, company)]
            own_items = [request.item_texts.get(i, "") for i in request.item_texts
                         if ticker in {t.upper() for t in request.item_tickers.get(i, ())}]
            failures += check_date(lab.get("scheduled_event"), own_pages + own_items, et_date(now))
            hostile = check_hostile(lab, item_texts + pages)
            if hostile:
                failures, lab, citations = hostile, None, []
            if lab is not None:
                lab = dict(lab, evidence_ids=[request.item_ids.get(e) or e for e in lab.get("evidence_ids") or ()])
        else:
            failures.append(f"call_{outcome.validation or outcome.reason}")
        label = build_label(request, outcome, lab, fact_id=None, failures=failures, labelled_at=iso(self.clock.now()),
                            cost_usd=outcome.cost_usd, urls=tuple(str(c.get("url")) for c in citations),
                            quotes=tuple(str(c.get("quote")) for c in citations), ticker=ticker)
        write_label(self.paths, label)
        return 1
