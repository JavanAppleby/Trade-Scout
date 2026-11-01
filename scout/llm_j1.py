"""J1 bulk tagging (SCOUT_SPEC 8.2, DESIGN 2.6 and D8).

Every `j1_interval_s` in PRE/CORE/POST: up to `j1_batch_size` new wire and filing facts with a
ticker, read from `state/facts/`, not yet labelled by J1, become one request. Scout has no price feed
(D8): a fact is skipped only when a Radar find says its ticker trades above `scan_max_price`. One
label per (ticker, fact) goes to `state/labels/`; a fact the model left out, or whose reply failed,
is recorded `unlabelled`, so it is never sent twice. Labels never change an answer.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from scout.calendar import Session, session_at
from scout.clock import Clock, et_date, iso, parse_iso
from scout.config import ScoutConfig
from scout.inputs import radar_finds
from scout.llm_checks import check_date, check_hostile, check_label, injection_suspected
from scout.llm_client import Outcome, execute
from scout.llm_ledger import Ledger
from scout.llm_prompts import LlmRequest, build_j1, clean_ticker
from scout.llm_records import build_label, labels_on, write_label
from scout.paths import ScoutPaths
from scout.store import read_jsonl

ACTIVE = (Session.PRE, Session.CORE, Session.POST)
LABEL_KINDS = ("wire", "filing")

__all__ = ["J1Job", "apply_labels", "pending_facts"]


def _days(now: datetime) -> list:
    today = et_date(now)
    return [today - timedelta(days=1), today]


def labelled_fact_ids(paths: ScoutPaths, now: datetime, job: str = "J1") -> set[str]:
    out: set[str] = set()
    for day in _days(now):
        out.update(str(r.get("fact_id")) for r in labels_on(paths, day) if r.get("job") == job and r.get("fact_id"))
    return out


def pending_facts(cfg: ScoutConfig, paths: ScoutPaths, now: datetime) -> tuple[list[dict[str, Any]],
                                                                              dict[str, list[str]]]:
    """(facts to label, oldest first; fact id -> its release's ticker set)."""
    facts: list[dict[str, Any]] = []
    for day in _days(now):
        facts.extend(read_jsonl(paths.day_file(paths.facts, day)))
    by_release: dict[tuple[str, str], set[str]] = {}
    for f in facts:
        t = clean_ticker(f.get("ticker"))
        if t:
            by_release.setdefault((str(f.get("source")), str(f.get("guid"))), set()).add(t)
    too_dear = {f.symbol for f in radar_finds(cfg, et_date(now))
                if f.price is not None and f.price > cfg.scan_max_price}
    done = labelled_fact_ids(paths, now)
    out, seen = [], set()
    for f in facts:
        fid = f.get("fact_id")
        seen_at = parse_iso(f.get("first_seen_at"))
        ticker = clean_ticker(f.get("ticker"))
        if (f.get("format") != 1 or not fid or fid in done or fid in seen or f.get("kind") not in LABEL_KINDS
                or ticker is None or ticker in too_dear or seen_at is None or seen_at > now):
            continue
        seen.add(fid)
        out.append(f)
    out.sort(key=lambda f: (str(f.get("first_seen_at")), str(f.get("fact_id"))))
    sets = {str(f["fact_id"]): sorted(by_release.get((str(f.get("source")), str(f.get("guid"))), set())) for f in out}
    return out, sets


def apply_labels(request: LlmRequest, outcome: Outcome, facts_by_item: Mapping[str, Mapping[str, Any]],
                 now: datetime) -> list:
    """One Label per input item, checked (SS 8.4). Pure: returns the Label records."""
    entries: dict[str, Any] = {}
    if outcome.ok and isinstance(outcome.obj, dict):
        for entry in outcome.obj.get("labels", []):
            entries.setdefault(str(entry.get("item_id")), entry.get("label"))
    n = max(1, len(facts_by_item))
    share = outcome.cost_usd / n
    # an instruction in any item reached the model with every other item of the request (SS 8.4)
    batch_hostile = any(injection_suspected(t) for t in request.item_texts.values())
    out = []
    for item_id, fact in facts_by_item.items():
        lab = entries.get(item_id)
        failures: list[str] = []
        if lab is None:
            failures.append("no_label_for_item" if outcome.ok else f"call_{outcome.validation or outcome.reason}")
        else:
            texts = [request.item_texts.get(e, "") for e in lab.get("evidence_ids") or ()] or [
                request.item_texts.get(item_id, "")]
            failures += check_label(lab, request.item_tickers.get(item_id, ()), request.item_ids.keys())
            if item_id not in (lab.get("evidence_ids") or ()):
                failures.append("evidence_missing_own_item")
            # a date quote must come from this item, or a cited item whose release names this ticker
            ticker = str(lab.get("ticker") or "").upper()
            date_texts = [request.item_texts.get(item_id, "")] + [
                request.item_texts.get(e, "") for e in lab.get("evidence_ids") or ()
                if e != item_id and ticker and ticker in {t.upper() for t in request.item_tickers.get(e, ())}]
            failures += check_date(lab.get("scheduled_event"), date_texts, et_date(now))
            hostile = check_hostile(lab, [request.item_texts.get(item_id, ""), *texts])
            if hostile:  # an instruction in the input: never a label other than promotion_suspected
                failures, lab = hostile, None
            elif batch_hostile:  # another item of this request tried to steer the model
                failures.append("injection_in_batch")
        if lab is not None:
            lab = dict(lab, evidence_ids=[request.item_ids.get(e) or e for e in lab.get("evidence_ids") or ()])
        out.append(build_label(request, outcome, lab, fact_id=str(fact.get("fact_id")), failures=failures,
                               labelled_at=iso(now), cost_usd=share, urls=(fact.get("url"),) if fact.get("url") else (),
                               published_at=fact.get("published_at"), ticker=fact.get("ticker")))
    return out


class J1Job:
    def __init__(self, cfg: ScoutConfig, paths: ScoutPaths, transport: Any, ledger: Ledger, clock: Clock, *,
                 log: logging.Logger | None = None) -> None:
        self.cfg, self.paths, self.transport, self.ledger, self.clock = cfg, paths, transport, ledger, clock
        self.log = log or logging.getLogger("scout.llm_j1")

    def run(self, now: datetime) -> int:
        """Label one batch. Returns the number of labels written (0 when off, outside PRE/CORE/POST,
        at a cap or with nothing new)."""
        if not self.cfg.llm_enabled or not self.cfg.llm_job("J1").enabled:
            return 0
        if session_at(now) not in ACTIVE or self.ledger.cap_reached("J1"):
            return 0
        facts, sets = pending_facts(self.cfg, self.paths, now)
        if not facts:
            return 0
        batch = facts[: self.cfg.j1_batch_size]
        request = build_j1(batch, self.cfg, ticker_sets=sets)
        outcome = execute(self.transport, request, self.ledger, self.paths, self.clock)
        if outcome.status == "refused":
            self.log.info("J1 not sent: %s", outcome.reason)
            return 0
        by_item = {item: f for item, f in zip(request.item_ids, batch, strict=False)}
        labels = apply_labels(request, outcome, by_item, self.clock.now())
        for label in labels:
            write_label(self.paths, label)
        return len(labels)
