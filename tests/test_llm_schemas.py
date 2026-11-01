"""Label schema v1 and the job output schemas (SCOUT_SPEC 8.1 item 3, 8.3)."""

from __future__ import annotations

from typing import Any

import pytest

from helpers_fake_anthropic import label
from scout.llm_schemas import (
    CATALYST_TYPES, CONFIDENCES, DILUTION_RISKS, DIRECTIONS, J1_OUTPUT, J3_OUTPUT, J4_OUTPUT, LABEL_SCHEMA_V1,
    SCHEMA_VERSION, validate,
)


def _objects(schema: Any) -> list[dict[str, Any]]:
    out = []
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            out.append(schema)
        for v in schema.values():
            out.extend(_objects(v))
    elif isinstance(schema, list):
        for v in schema:
            out.extend(_objects(v))
    return out


@pytest.mark.parametrize("schema", [LABEL_SCHEMA_V1, J1_OUTPUT, J3_OUTPUT, J4_OUTPUT])
def test_every_object_is_closed_and_fully_required(schema: dict[str, Any]) -> None:
    objects = _objects(schema)
    assert objects
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])


def test_spec_83_fields_and_enums() -> None:
    assert SCHEMA_VERSION == 1
    assert set(LABEL_SCHEMA_V1["properties"]) == {
        "ticker", "catalyst_type", "direction", "dilution_risk", "is_new_information", "scheduled_event",
        "confidence", "evidence_ids", "summary"}
    assert len(CATALYST_TYPES) == 20 and "promotion_suspected" in CATALYST_TYPES and "unclear" in CATALYST_TYPES
    assert DIRECTIONS == ("positive", "negative", "mixed", "neutral")
    assert DILUTION_RISKS == ("none", "low", "high", "unknown")
    assert CONFIDENCES == ("low", "med", "high")


def test_valid_label_passes() -> None:
    assert validate(label("ABC", ["F1"]), LABEL_SCHEMA_V1) == []
    event = {"kind": "earnings", "date": "2026-11-12", "quote": "will report results on November 12, 2026"}
    assert validate(label("ABC", ["F1"], scheduled_event=event), LABEL_SCHEMA_V1) == []


@pytest.mark.parametrize("mutate, needle", [
    (lambda d: d.update(extra_field=1), "outside the schema"),
    (lambda d: d.update(catalyst_type="fda_approval_confirmed"), "enum"),
    (lambda d: d.pop("summary"), "missing"),
    (lambda d: d.update(is_new_information="yes"), "expected boolean"),
    (lambda d: d.update(confidence=True), "expected string"),
    (lambda d: d.update(evidence_ids="F1"), "expected array"),
    (lambda d: d.update(evidence_ids=[1]), "expected string"),
    (lambda d: d.update(scheduled_event={"kind": "earnings", "date": "2026-11-12"}), "no allowed shape"),
    (lambda d: d.update(scheduled_event={"kind": "earnings", "date": "x", "quote": "y", "more": 1}),
     "no allowed shape"),
])
def test_schema_failures(mutate: Any, needle: str) -> None:
    lab = label("ABC", ["F1"])
    mutate(lab)
    errors = validate(lab, LABEL_SCHEMA_V1)
    assert errors and any(needle in e for e in errors)


def test_job_outputs() -> None:
    assert validate({"labels": [{"item_id": "F1", "label": label("A", ["F1"])}]}, J1_OUTPUT) == []
    assert validate({"labels": [{"item_id": "F1", "label": label("A", ["F1"]), "note": "x"}]}, J1_OUTPUT)
    assert validate({"label": label("A", []), "citations": [{"url": "https://sec.gov/x", "quote": "q"}]},
                    J3_OUTPUT) == []
    assert validate({"paragraph": "p", "watch_list": [{"ticker": "A", "reason": "r", "evidence_ids": ["C1"]}]},
                    J4_OUTPUT) == []
    assert validate([], J4_OUTPUT) == ["$: expected object"]
