"""Jason's level files: the price grammar and the setup parser (SCOUT_SPEC 5.3, DESIGN 2.4).

Provenance: ported from `ai-trading-platform` `config/jason_levels.py` (767 lines), commit
d62a282659f4c70fba7450471a2add98eef5af82, file sha256
a5f541f29cad5e496045b909feea2e3be5008d98d5ad16b41364d1a68bcd8f9c. The parsing half lives here and
the loader and queries in `scout/levels.py`. The private names of the source (`_one_price`,
`_prices_in`, `_level_readings`, `_parse_levels`, `_parse_conditionals`, `_parse_setup`) are public
here without the underscore. Every rule that changes behaviour is kept:

- A price is what is literally written: `$`, thousands commas, decimals and a `K` multiplier. The
  transcriber's repair brackets (`6[5]`) come off first.
- A range or a pair (" to ", "-" between digits, ", " with a space) gives several readings and the
  level is ambiguous. `2,085.84` (no space after the comma) is one price.
- A decade ("330s") or a gap ("64 30") is refused and named: that is a judgement, not a value. The
  fix is `price_alternatives`, which add readings (a human's resolution is honoured).
- An ambiguous level becomes one level per reading, each carrying every alternative.
- A bad level or setup is dropped and named in the warnings; it never raises.
- Unknown direction reads `neutral`; missing or unknown attribution reads `unclear`; a bad `expires`
  falls back to the default; an expiry before the session date is warned about ("born stale").

Changed for Scout: each expanded level also carries `position` (its index in the file's `levels`
list) so a snapshot can regroup the readings of one declared level. Pure: no IO, no clock.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation

__all__ = [
    "ATTRIBUTIONS", "DEFAULT_ACCEPT_MINUTES", "DEFAULT_ATTRIBUTION", "DEFAULT_EXPIRY_DAYS", "DEFAULT_SOURCE_KIND",
    "DIRECTIONS", "KINDS", "KIND_INVALIDATION", "KIND_RESISTANCE", "KIND_RE_ENTRY", "KIND_SUPPORT", "KIND_TARGET",
    "KIND_TRIGGER", "SOURCE_KINDS", "dec", "headline", "iso_date", "level_readings", "one_price",
    "parse_conditionals", "parse_levels", "parse_setup", "prices_in", "text",
]

KIND_SUPPORT = "support"
KIND_RESISTANCE = "resistance"
KIND_TRIGGER = "trigger"
KIND_TARGET = "target"
KIND_INVALIDATION = "invalidation"
KIND_RE_ENTRY = "re-entry"
KINDS = (KIND_SUPPORT, KIND_RESISTANCE, KIND_TRIGGER, KIND_TARGET, KIND_INVALIDATION, KIND_RE_ENTRY)

DIRECTIONS = ("long", "short", "neutral", "avoid")
ATTRIBUTIONS = ("jason", "amit", "unclear")
#: A setup with no attribution is `unclear`, never `jason`: the default fails toward "not his".
DEFAULT_ATTRIBUTION = "unclear"

#: How long a read lives when neither the setup nor the file dates it (session_date + 7 days).
DEFAULT_EXPIRY_DAYS = 7
#: The file's own `accept_minutes` default (carried for the record; Scout watches no prices).
DEFAULT_ACCEPT_MINUTES = 15

#: SCOUT_SPEC 5.1: the one added top-level key. A file without it reads as a Tuesday read.
SOURCE_KINDS = ("tuesday", "weekly", "daily")
DEFAULT_SOURCE_KIND = "tuesday"


# --------------------------------------------------------------------------- small readers


def dec(value: object) -> Decimal | None:
    """A positive Decimal, or None. Never raises."""
    try:
        out = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, TypeError, ValueError):
        return None
    return out if out.is_finite() and out > 0 else None


def iso_date(value: object) -> str | None:
    """`YYYY-MM-DD` when `value` is an ISO date, else None."""
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except (AttributeError, TypeError, ValueError):
        return None


def text(value: object) -> str:
    return "" if value is None else str(value).strip()


#: Keys that give a one-line headline when `market` or `source` is an object, not a string.
_HEADLINE_KEYS = ("headline", "stream_title", "title", "summary", "name")


def headline(value: object) -> str:
    """A one-line description, whether the field is a string or a block (descriptive only)."""
    if isinstance(value, Mapping):
        for key in _HEADLINE_KEYS:
            if value.get(key):
                return text(value[key])
        return ""
    return text(value)


# --------------------------------------------------------------------------- the price grammar

#: A bare price: optional `$`, digits with optional thousands groups and decimals, optional `K`.
_PRICE_TOKEN = re.compile(r"^\$?(\d{1,3}(?:,\d{3})*|\d+)(\.\d+)?\s*([Kk])?$")
#: `130 to 125`, `65-70`, `325, 320`. The comma form needs the space, so `2,085.84` stays one price.
_RANGE_SPLIT = re.compile(r"\s+to\s+|\s*(?<=\d)-(?=[\$\d])\s*|,\s+")


def one_price(token: str) -> Decimal | None:
    """One written-out price, or None. It interprets nothing: `330s` (a decade) and `64 30` (a gap)
    are refused, because reading them needs to know what the speaker meant."""
    match = _PRICE_TOKEN.match(str(token).strip().replace("[", "").replace("]", ""))
    if not match:
        return None
    whole, fraction, thousands = match.groups()
    try:
        value = Decimal(whole.replace(",", "") + (fraction or ""))
    except (InvalidOperation, ValueError):  # pragma: no cover - the regex admits only digits
        return None
    if thousands:
        value *= 1000
    return value if value > 0 else None


def prices_in(raw_text: object) -> tuple[list[Decimal], bool, str | None]:
    """Every price written in one `price` field: `(readings, is_a_zone, problem)`.

    A range or a pair gives both ends and marks the level a zone (ambiguous). The repair brackets
    come off before splitting, so `6[5]-$70` is the range 65 to 70."""
    raw = str(raw_text or "").strip().replace("[", "").replace("]", "")
    if not raw:
        return [], False, "empty price"
    tokens = [t for t in _RANGE_SPLIT.split(raw) if t and t.strip()]
    if not tokens:
        return [], False, f"unreadable price {raw!r}"
    readings: list[Decimal] = []
    for token in tokens:
        parsed = one_price(token)
        if parsed is None:
            return [], False, (
                f"price {raw!r} is not written out as a number ({token.strip()!r}) - a decade like "
                f"'330s' or a gap like '64 30' is a judgement, not a value; give explicit "
                f"price_alternatives instead")
        readings.append(parsed)
    return readings, len(readings) > 1, None


def level_readings(raw: Mapping) -> tuple[list[Decimal], bool, list[str]]:
    """Every price this level could mean (sorted, unique), whether it is ambiguous, and problems.

    `ambiguous` with one reading stays ambiguous; alternatives without the flag are ambiguous too.
    An unreadable `price` with readable `price_alternatives` keeps the alternatives."""
    problems: list[str] = []
    primary, primary_is_zone, trouble = prices_in(raw.get("price"))
    if trouble:
        problems.append(trouble)
    alternatives: list[Decimal] = []
    raw_alts = raw.get("price_alternatives")
    if isinstance(raw_alts, (list, tuple)):
        for alt in raw_alts:
            parsed, _zone, alt_trouble = prices_in(alt)
            if alt_trouble:
                problems.append(f"price_alternative {alt!r}: {alt_trouble}")
            alternatives.extend(parsed)
    elif raw_alts is not None:
        problems.append("price_alternatives is not a list")
    flagged = bool(raw.get("ambiguous")) or bool(alternatives) or primary_is_zone
    readings = sorted(set(primary) | set(alternatives))
    return readings, flagged, problems


def parse_levels(symbol: str, raw_levels: object) -> tuple[list[dict], list[str]]:
    """Expand a setup's declared levels: one level per reading, each with every alternative."""
    problems: list[str] = []
    out: list[dict] = []
    if not isinstance(raw_levels, (list, tuple)):
        return out, [f"{symbol}: levels is not a list"]
    seen: set[str] = set()
    for position, raw in enumerate(raw_levels):
        if not isinstance(raw, Mapping):
            problems.append(f"{symbol}: levels[{position}] is not an object")
            continue
        kind = text(raw.get("kind")).lower()
        if kind not in KINDS:
            problems.append(f"{symbol}: levels[{position}] kind {kind!r} is not one of {', '.join(KINDS)}")
            continue
        readings, ambiguous, trouble = level_readings(raw)
        problems.extend(f"{symbol}: levels[{position}] {t}" for t in trouble)
        if not readings:
            if not trouble:  # one complaint per dropped level
                problems.append(f"{symbol}: levels[{position}] ({kind}) carries no readable price - dropped")
            continue
        alternatives = [str(r) for r in readings]
        for price in readings:
            level_id = f"{kind}@{price}"
            if level_id in seen:
                continue  # the same reading declared twice is one level
            seen.add(level_id)
            out.append({
                "level_id": level_id,
                "kind": kind,
                "price": price,
                "note": text(raw.get("note")),
                "timestamp": text(raw.get("timestamp")),
                "ambiguous": ambiguous,
                "alternatives": alternatives if ambiguous else [],
                "position": position,
            })
    return out, problems


def parse_conditionals(symbol: str, raw: object) -> tuple[list[dict], list[str]]:
    """`if`/`then` pairs, carried verbatim and never evaluated (they are prose)."""
    problems: list[str] = []
    out: list[dict] = []
    if raw is None:
        return out, problems
    if not isinstance(raw, (list, tuple)):
        return out, [f"{symbol}: conditionals is not a list"]
    for position, item in enumerate(raw):
        if not isinstance(item, Mapping):
            problems.append(f"{symbol}: conditionals[{position}] is not an object")
            continue
        out.append({"if": text(item.get("if")), "then": text(item.get("then"))})
    return out, problems


def parse_setup(raw: object, *, session_date: str, default_expires: str,
                source_kind: str = DEFAULT_SOURCE_KIND) -> tuple[dict | None, list[str]]:
    """One setup, or None when it has no symbol or is not an object; plus its problems."""
    if not isinstance(raw, Mapping):
        return None, ["a setup that is not an object - dropped"]
    symbol = text(raw.get("symbol")).upper()
    if not symbol:
        return None, ["a setup without a symbol - dropped"]

    problems: list[str] = []
    direction = text(raw.get("direction")).lower() or "neutral"
    if direction not in DIRECTIONS:
        problems.append(f"{symbol}: direction {direction!r} is not one of {', '.join(DIRECTIONS)} - read as "
                        f"'neutral'")
        direction = "neutral"

    attribution = text(raw.get("attribution")).lower() or DEFAULT_ATTRIBUTION
    if attribution not in ATTRIBUTIONS:
        problems.append(f"{symbol}: attribution {attribution!r} is not one of {', '.join(ATTRIBUTIONS)} - read "
                        f"as {DEFAULT_ATTRIBUTION!r}")
        attribution = DEFAULT_ATTRIBUTION

    levels, trouble = parse_levels(symbol, raw.get("levels"))
    problems.extend(trouble)
    conditionals, trouble = parse_conditionals(symbol, raw.get("conditionals"))
    problems.extend(trouble)

    expires = iso_date(raw.get("expires"))
    if expires is None:
        if raw.get("expires") is not None:
            problems.append(f"{symbol}: expires {raw.get('expires')!r} is not an ISO date - defaulted to "
                            f"{default_expires}")
        expires = default_expires
    if expires < session_date:
        problems.append(f"{symbol}: expires {expires} precedes the session date {session_date} - the setup is "
                        f"born stale and is never current")

    return {
        "symbol": symbol,
        "direction": direction,
        "attribution": attribution,
        "confidence": text(raw.get("confidence")),
        "horizon": text(raw.get("horizon")),
        "expires": expires,
        "levels": levels,
        "conditionals": conditionals,
        "session_date": session_date,
        "source_kind": source_kind,
    }, problems
