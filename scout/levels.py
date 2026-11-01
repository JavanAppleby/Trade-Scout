"""Jason's level files: the loader, the folder merge and the queries (SCOUT_SPEC 5.1-5.3, DESIGN 2.4).

Provenance: ported from `ai-trading-platform` `config/jason_levels.py` (767 lines), commit
d62a282659f4c70fba7450471a2add98eef5af82, file sha256
a5f541f29cad5e496045b909feea2e3be5008d98d5ad16b41364d1a68bcd8f9c. The price grammar is in
`scout/levels_parse.py`.

Rules kept from the source:
- A bad file raises `JasonPlanError` (missing, unreadable, not an object, no ISO `session_date`, no
  `setups` list). Everything else is a warning and the rest of the read loads.
- `expires_default` in the file wins over session_date + 7 days.
- A symbol declared twice: the later setup wins, with a warning.
- `conflict_for` and `invalidation_reference` return records only, never a veto. An absent or
  unreadable plan is never a conflict.

Removed (SCOUT_SPEC 5.3): `_platform_view` and its `research.levels` import, the hard-coded
`PLAN_PATH` (the folder is `levels_dir`), and `feedable_setups` with `FEEDABLE_*` (Scout feeds no
trade path). `today` is always passed in (Scout never reads the system date here).

Added: the top-level `source_kind` ("tuesday" | "weekly" | "daily"; missing or unknown reads
"tuesday" with a warning) carried onto every setup, and `load_folder(levels_dir, today)`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from scout.levels_parse import (
    DEFAULT_ACCEPT_MINUTES, DEFAULT_EXPIRY_DAYS, DEFAULT_SOURCE_KIND, KIND_INVALIDATION, KIND_RESISTANCE,
    KIND_SUPPORT, KIND_TRIGGER, SOURCE_KINDS, dec, headline, iso_date, parse_setup, text,
)

__all__ = [
    "META_SUFFIX", "SOURCE_KIND_ORDER", "JasonPlanError", "conflict_for", "current_setups", "describe_levels",
    "invalidation_reference", "level_files", "levels_of", "load_folder", "load_plan", "plan_is_current",
    "setup_for", "setup_is_current", "symbols",
]

#: The sidecar Add Levels writes next to each level file; never a level file itself.
META_SUFFIX = ".meta.json"
#: When two current files share a session date, the more specific read comes first.
SOURCE_KIND_ORDER = {"daily": 0, "weekly": 1, "tuesday": 2}


class JasonPlanError(ValueError):
    """The level file is absent, unreadable, or its top level does not match the schema. A single
    bad setup or level never raises."""


Today = date | str


def _iso(today: Today) -> str:
    return today if isinstance(today, str) else today.isoformat()


# --------------------------------------------------------------------------- loading


def load_plan(path: Path | str) -> dict:
    """Read and validate one level file.

    Raises `JasonPlanError` only when the file cannot be read or has no ISO `session_date` or no
    `setups` list. Returns `{session_date, source_kind, expires_default, source, market,
    accept_minutes, setups, warnings}`; unknown top-level keys are ignored."""
    path = Path(path)
    if not path.is_file():
        raise JasonPlanError(f"{path.name} does not exist")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise JasonPlanError(f"{path.name} is unreadable: {exc}") from exc
    return plan_from_data(data, name=path.name)


def plan_from_data(data: object, *, name: str = "level file") -> dict:
    """`load_plan` over already-parsed JSON (the same rules and errors)."""
    if not isinstance(data, Mapping):
        raise JasonPlanError(f"{name}: top level must be an object")
    session_date = iso_date(data.get("session_date"))
    if session_date is None:
        raise JasonPlanError(
            f"{name}: session_date is missing or not an ISO date ({data.get('session_date')!r}) - a read with "
            f"no date cannot be aged out")
    raw_setups = data.get("setups")
    if not isinstance(raw_setups, (list, tuple)):
        raise JasonPlanError(f"{name}: setups must be a list")

    warnings: list[str] = []
    source_kind = text(data.get("source_kind")).lower()
    if not source_kind:
        warnings.append(f"source_kind is missing - read as {DEFAULT_SOURCE_KIND!r}")
        source_kind = DEFAULT_SOURCE_KIND
    elif source_kind not in SOURCE_KINDS:
        warnings.append(f"source_kind {source_kind!r} is not one of {', '.join(SOURCE_KINDS)} - read as "
                        f"{DEFAULT_SOURCE_KIND!r}")
        source_kind = DEFAULT_SOURCE_KIND

    default_expires = iso_date(data.get("expires_default"))
    if default_expires is None:
        if data.get("expires_default") is not None:
            warnings.append(f"expires_default {data.get('expires_default')!r} is not an ISO date - using "
                            f"session_date + {DEFAULT_EXPIRY_DAYS} days")
        default_expires = (date.fromisoformat(session_date) + timedelta(days=DEFAULT_EXPIRY_DAYS)).isoformat()

    accept_minutes = DEFAULT_ACCEPT_MINUTES
    raw_accept = data.get("accept_minutes")
    if raw_accept is not None:
        try:
            parsed = int(raw_accept)
        except (TypeError, ValueError):
            parsed = 0
        if parsed > 0:
            accept_minutes = parsed
        else:
            warnings.append(f"accept_minutes {raw_accept!r} is not a positive integer - using "
                            f"{DEFAULT_ACCEPT_MINUTES}")

    setups: list[dict] = []
    seen: dict[str, int] = {}
    for raw in raw_setups:
        setup, problems = parse_setup(raw, session_date=session_date, default_expires=default_expires,
                                      source_kind=source_kind)
        warnings.extend(problems)
        if setup is None:
            continue
        if setup["symbol"] in seen:  # a later correction in the stream is later in the file
            warnings.append(f"{setup['symbol']}: declared more than once - the later setup supersedes the "
                            f"earlier one")
            setups[seen[setup["symbol"]]] = setup
            continue
        seen[setup["symbol"]] = len(setups)
        setups.append(setup)

    return {
        "session_date": session_date,
        "source_kind": source_kind,
        "expires_default": default_expires,
        "source": headline(data.get("source")),
        "market": headline(data.get("market")),
        "accept_minutes": accept_minutes,
        "setups": setups,
        "warnings": warnings,
    }


def level_files(levels_dir: Path | str) -> list[Path]:
    """The level files in `levels_dir` (`*.json`, not sidecars, not hidden temp files), by name."""
    folder = Path(levels_dir)
    try:
        candidates = sorted(folder.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    return [p for p in candidates
            if p.is_file() and p.suffix.lower() == ".json" and not p.name.lower().endswith(META_SUFFIX)
            and not p.name.startswith(".")]


def _merge_key(setup: Mapping) -> tuple:
    # newest session_date first, then the more specific kind, then the later file name
    return (str(setup["symbol"]),
            -date.fromisoformat(str(setup["session_date"])).toordinal(),
            SOURCE_KIND_ORDER.get(str(setup.get("source_kind")), 9),
            tuple(-ord(c) for c in str(setup.get("file", ""))))


def load_folder(levels_dir: Path | str, today: Today) -> dict:
    """Merge every current level file in `levels_dir` (SCOUT_SPEC 5.3).

    Every setup current on `today` is kept with its `source_kind`, `session_date` and `file`. Per
    symbol they are ordered newest `session_date` first (then daily, weekly, tuesday), so
    `setup_for` answers from the newest read. A file that raises is skipped and named in `warnings`;
    a file's own warnings are prefixed with its name. Returns `{today, files, setups, warnings}`
    where `files` lists the current files (those with at least one current setup)."""
    today_iso = _iso(today)
    setups: list[dict] = []
    files: list[str] = []
    warnings: list[str] = []
    for path in level_files(levels_dir):
        try:
            plan = load_plan(path)
        except JasonPlanError as exc:
            warnings.append(f"{path.name}: refused - {exc}")
            continue
        warnings.extend(f"{path.name}: {w}" for w in plan["warnings"])
        current = [dict(s, file=path.name) for s in plan["setups"] if setup_is_current(s, today_iso)]
        if current:
            files.append(path.name)
            setups.extend(current)
    setups.sort(key=_merge_key)
    return {"today": today_iso, "files": files, "setups": setups, "warnings": warnings}


# --------------------------------------------------------------------------- currency and queries


def setup_is_current(setup: Mapping, today: Today) -> bool:
    """True while today is on or before the setup's own `expires`."""
    return _iso(today) <= str(setup.get("expires") or "")


def current_setups(plan: Mapping, today: Today) -> list[dict]:
    return [s for s in plan.get("setups", ()) if setup_is_current(s, today)]


def plan_is_current(plan: Mapping, today: Today) -> bool:
    """True while any setup is still current (a Tuesday read describes the rest of the week)."""
    return bool(current_setups(plan, today))


def setup_for(symbol: str, plan: Mapping, today: Today) -> dict | None:
    """The first current setup for `symbol` (the newest one in a merged folder plan)."""
    name = str(symbol or "").strip().upper()
    for setup in current_setups(plan, today):
        if setup["symbol"] == name:
            return setup
    return None


def levels_of(setup: Mapping, kind: str) -> list[dict]:
    return [lvl for lvl in setup.get("levels", ()) if lvl.get("kind") == kind]


def _lowest(levels: Sequence[Mapping]) -> dict | None:
    return min(levels, key=lambda lvl: lvl["price"]) if levels else None


def _highest(levels: Sequence[Mapping]) -> dict | None:
    return max(levels, key=lambda lvl: lvl["price"]) if levels else None


def _level_payload(level: Mapping | None) -> dict:
    if not level:
        return {}
    return {
        "jason_level": str(level["price"]),
        "jason_level_kind": level["kind"],
        "jason_level_note": level.get("note") or "",
        "jason_level_ambiguous": bool(level.get("ambiguous")),
        "jason_level_alternatives": list(level.get("alternatives") or ()),
    }


def _plan_or_none(plan: Mapping | None, path: Path | str | None) -> Mapping | None:
    if plan is not None:
        return plan
    if path is None:
        return None
    try:
        return load_plan(path)
    except JasonPlanError:  # "we could not read Jason's file" is never "Jason said no"
        return None


def conflict_for(symbol: str, price: Decimal | str | float | None = None, *, today: Today,
                 plan: Mapping | None = None, path: Path | str | None = None) -> dict | None:
    """Does Jason's current read disagree with being long `symbol` at `price`? A record, never a veto.

    None when there is no plan, no current setup for the symbol, or nothing to disagree about.
    Reason codes, in order: `jason_short` / `jason_avoid` (the read's direction);
    `below_jason_invalidation` (price under the highest invalidation, flagged sensitively); else,
    when the read named no invalidation, `below_jason_support` (price under every support, i.e.
    the lowest). The level on the record is the breached one, else the highest trigger, else the
    highest resistance."""
    plan = _plan_or_none(plan, path)
    if plan is None:
        return None
    setup = setup_for(symbol, plan, today)
    if setup is None:
        return None
    reasons: list[str] = []
    if setup["direction"] in ("short", "avoid"):
        reasons.append(f"jason_{setup['direction']}")
    parsed_price = dec(price) if price is not None else None
    invalidations = levels_of(setup, KIND_INVALIDATION)
    breached = None
    if parsed_price is not None:
        if invalidations:
            candidate = _highest(invalidations)
            if candidate is not None and parsed_price < candidate["price"]:
                breached = candidate
                reasons.append("below_jason_invalidation")
        else:
            candidate = _lowest(levels_of(setup, KIND_SUPPORT))
            if candidate is not None and parsed_price < candidate["price"]:
                breached = candidate
                reasons.append("below_jason_support")
    if not reasons:
        return None
    level = breached or _highest(levels_of(setup, KIND_TRIGGER)) or _highest(levels_of(setup, KIND_RESISTANCE))
    return {
        "symbol": setup["symbol"],
        "jason_conflict": reasons[0],
        "jason_conflict_reasons": reasons,
        "jason_direction": setup["direction"],
        "jason_attribution": setup["attribution"],
        "jason_confidence": setup.get("confidence", ""),
        "jason_horizon": setup.get("horizon", ""),
        "jason_session_date": setup["session_date"],
        "jason_source_kind": setup.get("source_kind", DEFAULT_SOURCE_KIND),
        "jason_expires": setup["expires"],
        "jason_price_seen": str(parsed_price) if parsed_price is not None else None,
        **_level_payload(level),
    }


def invalidation_reference(symbol: str, *, today: Today, plan: Mapping | None = None,
                           path: Path | str | None = None) -> dict | None:
    """The lowest invalidation (else the lowest support) of the current read: a record only.

    None when there is no plan, no current setup, or the read named no downside level."""
    plan = _plan_or_none(plan, path)
    if plan is None:
        return None
    setup = setup_for(symbol, plan, today)
    if setup is None:
        return None
    level = _lowest(levels_of(setup, KIND_INVALIDATION)) or _lowest(levels_of(setup, KIND_SUPPORT))
    if level is None:
        return None
    return {
        "symbol": setup["symbol"],
        "jason_invalidation": str(level["price"]),
        "jason_invalidation_kind": level["kind"],
        "jason_invalidation_note": level.get("note") or "",
        "jason_invalidation_ambiguous": bool(level.get("ambiguous")),
        "jason_invalidation_alternatives": list(level.get("alternatives") or ()),
        "jason_attribution": setup["attribution"],
        "jason_direction": setup["direction"],
        "jason_session_date": setup["session_date"],
        "jason_source_kind": setup.get("source_kind", DEFAULT_SOURCE_KIND),
        "jason_expires": setup["expires"],
    }


def symbols(plan: Mapping, today: Today) -> list[str]:
    """Every symbol with a current read, sorted."""
    return sorted({s["symbol"] for s in current_setups(plan, today)})


def describe_levels(levels: Iterable[Mapping]) -> str:
    """`support 213 / support 190 / resistance 200` for a report line."""
    return " / ".join(f"{lvl['kind']} {lvl['price']}" + (" (ambiguous)" if lvl.get("ambiguous") else "")
                      for lvl in levels) or "no levels"
