"""LLM call and label records (SCOUT_SPEC 8.1 item 4, 10.2; DESIGN 5.7).

`state/llm/<ET date of at>.jsonl` holds one record per call (or per call refused before it was
sent); `state/labels/<ET date of labelled_at>.jsonl` one record per label. Both append-only.
Replays read these records and never call the model again.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from scout.clock import et_date, parse_iso
from scout.models import Label, LlmCall
from scout.paths import ScoutPaths
from scout.store import append_jsonl, read_jsonl

__all__ = ["build_label", "calls_on", "label_file", "labels_on", "llm_file", "write_call", "write_label"]


def llm_file(paths: ScoutPaths, day: date) -> Path:
    return paths.day_file(paths.llm, day)


def label_file(paths: ScoutPaths, day: date) -> Path:
    return paths.day_file(paths.labels, day)


def _day(stamp: str) -> date:
    at = parse_iso(stamp)
    if at is None:
        raise ValueError("record time is not an aware ISO time")
    return et_date(at)


def write_call(paths: ScoutPaths, call: LlmCall, *, reason: str | None = None) -> Path:
    """Append one call record. `reason` (an added key) says which cap refused a call."""
    rec: dict[str, Any] = call.to_record()
    rec["reason"] = reason
    target = llm_file(paths, _day(call.at))
    append_jsonl(paths, target, rec)
    return target


def write_label(paths: ScoutPaths, label: Label) -> Path:
    target = label_file(paths, _day(label.labelled_at))
    append_jsonl(paths, target, label.to_record())
    return target


def calls_on(paths: ScoutPaths, day: date) -> list[dict[str, Any]]:
    return read_jsonl(llm_file(paths, day))


def labels_on(paths: ScoutPaths, day: date) -> list[dict[str, Any]]:
    return read_jsonl(label_file(paths, day))


def build_label(request: Any, outcome: Any, label_obj: Any, *, fact_id: str | None, failures: list[str],
                labelled_at: str, cost_usd: float, urls: tuple[str, ...] = (), quotes: tuple[str, ...] = (),
                published_at: str | None = None, ticker: str | None = None) -> Label:
    """A Label record. `label_obj` is the schema-valid label from the model, or None for an
    unlabelled one. Scout, not the model, adds every metadata field (SS 8.3)."""
    from scout.llm_schemas import SCHEMA_VERSION  # noqa: PLC0415

    lab = label_obj if isinstance(label_obj, dict) else {}
    status = "labelled" if lab else "unlabelled"
    evidence = tuple(str(e) for e in lab.get("evidence_ids") or ())
    return Label(
        job=request.job, fact_id=fact_id or None, status=status,
        ticker=lab.get("ticker") if lab else ticker,
        catalyst_type=lab.get("catalyst_type"), direction=lab.get("direction"),
        dilution_risk=lab.get("dilution_risk"), is_new_information=lab.get("is_new_information"),
        scheduled_event=lab.get("scheduled_event"), confidence=lab.get("confidence"), evidence_ids=evidence,
        summary=lab.get("summary"), verified=bool(lab) and not failures, check_failures=tuple(failures),
        model_id=request.model, prompt_sha256=request.prompt_sha256, schema_version=SCHEMA_VERSION,
        request_id=getattr(outcome, "request_id", None), usage=dict(getattr(outcome, "usage", {}) or {}),
        cost_usd=round(float(cost_usd), 8), labelled_at=labelled_at, urls=tuple(urls), quotes=tuple(quotes),
        published_at=published_at, trust="layer2")
