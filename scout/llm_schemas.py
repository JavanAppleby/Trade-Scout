"""Label schema v1 and the job output schemas (SCOUT_SPEC 8.3, DESIGN 2.6).

Every object schema has `additionalProperties: false` and lists every property as required (the
structured-output rules); every category is an enum. `validate` is a small stdlib validator for
the subset used here (object, array, string with enum, boolean, null, anyOf, required,
additionalProperties), so a reply is checked again after the API's own constrained decoding: a
field outside the schema, or a value outside an enum, is a schema failure and the reply is
stored as `unlabelled` after at most one retry.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SCHEMA_VERSION = 1

CATALYST_TYPES = (
    "fda_decision", "pdufa_date_set", "trial_result", "contract_or_partnership", "earnings", "guidance",
    "offering_or_dilution", "reverse_split", "merger_or_acquisition", "delisting_notice", "uplisting",
    "insider_or_13d", "analyst", "crypto_or_treasury_pivot", "legal_regulatory", "product", "macro_or_sector",
    "promotion_suspected", "none", "unclear",
)
DIRECTIONS = ("positive", "negative", "mixed", "neutral")
DILUTION_RISKS = ("none", "low", "high", "unknown")
CONFIDENCES = ("low", "med", "high")
EVENT_KINDS = ("earnings", "fda_decision", "pdufa", "trial_readout", "shareholder_meeting", "offering_close",
               "reverse_split_effective", "conference", "other")

__all__ = [
    "CATALYST_TYPES", "CONFIDENCES", "DILUTION_RISKS", "DIRECTIONS", "EVENT_KINDS", "J1_OUTPUT", "J3_OUTPUT",
    "J4_OUTPUT", "LABEL_FIELDS", "LABEL_SCHEMA_V1", "OUTPUTS", "SCHEMA_VERSION", "validate",
]


def _obj(props: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": dict(props), "required": list(props), "additionalProperties": False}


def _enum(values: tuple[str, ...]) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": {"type": "string"}}

SCHEDULED_EVENT = {"anyOf": [
    _obj({"kind": _enum(EVENT_KINDS), "date": {"type": "string", "description": "YYYY-MM-DD"},
          "quote": {"type": "string", "description": "the sentence that states the date, copied verbatim"}}),
    {"type": "null"},
]}

LABEL_SCHEMA_V1: dict[str, Any] = _obj({
    "ticker": _STR,
    "catalyst_type": _enum(CATALYST_TYPES),
    "direction": _enum(DIRECTIONS),
    "dilution_risk": _enum(DILUTION_RISKS),
    "is_new_information": {"type": "boolean"},
    "scheduled_event": SCHEDULED_EVENT,
    "confidence": _enum(CONFIDENCES),
    "evidence_ids": _STR_LIST,
    "summary": _STR,
})
LABEL_FIELDS = tuple(LABEL_SCHEMA_V1["properties"])

# J1: one label per input item (the item id says which (ticker, fact) it labels).
J1_OUTPUT: dict[str, Any] = _obj({
    "labels": {"type": "array", "items": _obj({"item_id": _STR, "label": LABEL_SCHEMA_V1})},
})

# J3: one label for the symbol plus the URLs and verbatim quotes it rests on.
J3_OUTPUT: dict[str, Any] = _obj({
    "label": LABEL_SCHEMA_V1,
    "citations": {"type": "array", "items": _obj({"url": _STR, "quote": _STR})},
})

# J4: a paragraph and a watch list with evidence ids, display only.
J4_OUTPUT: dict[str, Any] = _obj({
    "paragraph": _STR,
    "watch_list": {"type": "array", "items": _obj({"ticker": _STR, "reason": _STR, "evidence_ids": _STR_LIST})},
})

OUTPUTS: dict[str, dict[str, Any]] = {"J1": J1_OUTPUT, "J3": J3_OUTPUT, "J4": J4_OUTPUT}

_TYPES: dict[str, tuple[type, ...]] = {"object": (dict,), "array": (list,), "string": (str,), "boolean": (bool,),
                                       "null": (type(None),)}


def validate(obj: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """Problems with `obj` against `schema` (empty when it conforms)."""
    if "anyOf" in schema:
        options = schema["anyOf"]
        results = [validate(obj, opt, path) for opt in options]
        if any(not r for r in results):
            return []
        return [f"{path}: matches no allowed shape"] + min(results, key=len)
    kind = schema.get("type")
    allowed = _TYPES.get(str(kind))
    if allowed is None:
        return [f"{path}: unsupported schema type {kind!r}"]
    if not isinstance(obj, allowed) or (kind != "boolean" and isinstance(obj, bool)):
        return [f"{path}: expected {kind}"]
    errors: list[str] = []
    if kind == "string" and "enum" in schema and obj not in schema["enum"]:
        errors.append(f"{path}: value not in enum")
    elif kind == "array":
        items = schema.get("items")
        if items is not None:
            for i, item in enumerate(obj):
                errors.extend(validate(item, items, f"{path}[{i}]"))
    elif kind == "object":
        props: Mapping[str, Any] = schema.get("properties", {})
        for key in schema.get("required", ()):
            if key not in obj:
                errors.append(f"{path}: missing {key!r}")
        for key, value in obj.items():
            if key in props:
                errors.extend(validate(value, props[key], f"{path}.{key}"))
            elif schema.get("additionalProperties", True) is False:
                errors.append(f"{path}: field outside the schema {str(key)[:40]!r}")
    return errors
