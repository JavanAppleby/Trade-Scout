"""The `[pace]` table of config.toml: how far each polled feed may slow down (owner, 2026-11-10;
DESIGN 21). Split from `scout.config` to keep each module small.

    [pace]
    enabled = true            false: every source keeps its fixed spacing (the behaviour before 0.2.4)
    factor = 0.5              poll about every factor x the typical time between a feed's new items
    lookback_days = 7         measured over this many ET days of Scout's own poll records
    recalc_time_et = "03:30"  recalculated daily at this ET time (and at start when the saved pace is old)
    min_hours = 2.0           an hour polled for less than this in the lookback keeps the fixed spacing
    page_fill = 0.5           never so slow that the busiest poll seen in that hour would fill more than
                              this share of the feed's page (`max_items`)
    [pace.slowest_active_s]   the slowest a feed may be polled inside `active_hours_et` on trading days
    [pace.slowest_idle_s]     ... and at every other time (nights, weekends, market holidays)

The two tables are keyed "source" or "source:FORM" (one EDGAR form); a form's own line wins over its
source's. A feed with no line, or 0, is not paced. A table given in config.toml replaces the whole
default table, as `[host_min_spacing_s]` does. Pacing never polls faster than a source's own
`min_spacing_s` / `idle_spacing_s`, so a value below that has no effect.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time
from types import MappingProxyType
from typing import Any

from scout.config_load import ConfigError, as_bool, as_float, as_int, as_table, as_time, check_keys

__all__ = ["DEFAULT_SLOWEST_ACTIVE_S", "DEFAULT_SLOWEST_IDLE_S", "PACED_KINDS", "PaceConfig", "pace_from_table",
           "validate_pace"]

#: Only the pollers that run on the shared schedule can be paced (the halt feed is never: a halt block
#: lasts only as long as the halt, so it keeps its 60 s; owner, 2026-11-10).
PACED_KINDS = ("wire", "filing")
SLOWEST_RANGE_S = (30.0, 3600.0)

# Owner, 2026-11-10. Inside trading hours (when Radar asks for blocks): a wire release is usually the
# first public notice of an offering, reverse split, listing notice or bankruptcy, so the wires stay
# within 2 minutes; filings usually follow the release of the same news, so the forms that can block a
# buy stay within 5 minutes; the forms read only for the evening hold answers and checks (NT 10-Q,
# NT 10-K, 10-Q, 10-K) within 30 minutes. Outside trading hours Radar asks for nothing, so every
# feed may wait up to 30 minutes (`source_overlap_max_gap_s`, the longest pause one repeated item
# covers); the page guard keeps a busy night hour fast.
DEFAULT_SLOWEST_ACTIVE_S: dict[str, float] = {
    "edgar_atom": 300.0, "edgar_atom:NT 10-Q": 1800.0, "edgar_atom:NT 10-K": 1800.0, "edgar_atom:10-Q": 1800.0,
    "edgar_atom:10-K": 1800.0, "prnewswire": 120.0, "stocktitan": 120.0,
}
DEFAULT_SLOWEST_IDLE_S: dict[str, float] = {"edgar_atom": 1800.0, "prnewswire": 1800.0, "stocktitan": 1800.0}

_KEYS = frozenset({"enabled", "factor", "lookback_days", "recalc_time_et", "min_hours", "page_fill",
                   "slowest_active_s", "slowest_idle_s"})


def _frozen(d: Mapping[str, float]) -> Mapping[str, float]:
    return MappingProxyType(dict(d))


@dataclass(frozen=True)
class PaceConfig:
    enabled: bool = True
    factor: float = 0.5
    lookback_days: int = 7
    recalc_time_et: time = time(3, 30)
    min_hours: float = 2.0
    page_fill: float = 0.5
    slowest_active_s: Mapping[str, float] = field(default_factory=lambda: _frozen(DEFAULT_SLOWEST_ACTIVE_S))
    slowest_idle_s: Mapping[str, float] = field(default_factory=lambda: _frozen(DEFAULT_SLOWEST_IDLE_S))

    def slowest(self, source: str, feed: str, active: bool) -> float | None:
        """The slowest spacing allowed for this feed now, or None when the feed is not paced."""
        table = self.slowest_active_s if active else self.slowest_idle_s
        value = table.get(f"{source}:{feed}")
        if value is None:
            value = table.get(source)
        return float(value) if value else None

    def paced(self, source: str) -> bool:
        """True when any line of either table names `source` (alone or with a form)."""
        return any(k == source or k.startswith(source + ":") for t in (self.slowest_active_s, self.slowest_idle_s)
                   for k, v in t.items() if v)

    def plan_inputs(self) -> dict[str, Any]:
        """The settings a saved plan was made with: a change to one of them is recalculated at start."""
        return {"factor": self.factor, "lookback_days": self.lookback_days, "min_hours": self.min_hours,
                "page_fill": self.page_fill}


def _slowest_table(name: str, value: Any) -> Mapping[str, float]:
    table = as_table(name, value)
    return _frozen({str(k): as_float(f"{name}.{k}", v) for k, v in table.items()})


def pace_from_table(value: Any) -> PaceConfig:
    """The `[pace]` table -> PaceConfig (every key optional; unknown keys are errors)."""
    data = as_table("pace", value)
    check_keys("pace", data, _KEYS)
    kwargs: dict[str, Any] = {}
    if "enabled" in data:
        kwargs["enabled"] = as_bool("pace.enabled", data["enabled"])
    for key in ("factor", "min_hours", "page_fill"):
        if key in data:
            kwargs[key] = as_float(f"pace.{key}", data[key])
    if "lookback_days" in data:
        kwargs["lookback_days"] = as_int("pace.lookback_days", data["lookback_days"])
    if "recalc_time_et" in data:
        kwargs["recalc_time_et"] = as_time("pace.recalc_time_et", data["recalc_time_et"])
    for key in ("slowest_active_s", "slowest_idle_s"):
        if key in data:
            kwargs[key] = _slowest_table(f"pace.{key}", data[key])
    return PaceConfig(**kwargs)


def _check(ok: bool, message: str) -> None:
    if not ok:
        raise ConfigError(message)


def validate_pace(c: Any) -> None:
    """Ranges and names of a built ScoutConfig's `pace` (called by `scout.config_validate.validate`)."""
    p: PaceConfig = c.pace
    _check(0.05 <= p.factor <= 1.0, f"pace.factor: must be in [0.05, 1.0], got {p.factor}")
    _check(2 <= p.lookback_days <= 60, f"pace.lookback_days: must be in [2, 60], got {p.lookback_days}")
    _check(0.25 <= p.min_hours <= 1000, f"pace.min_hours: must be in [0.25, 1000], got {p.min_hours}")
    _check(0.05 <= p.page_fill <= 1.0, f"pace.page_fill: must be in [0.05, 1.0], got {p.page_fill}")
    low, high = SLOWEST_RANGE_S
    for name, defaults in (("slowest_active_s", DEFAULT_SLOWEST_ACTIVE_S), ("slowest_idle_s", DEFAULT_SLOWEST_IDLE_S)):
        for key, value in getattr(p, name).items():
            where = f"pace.{name}.{key}"
            _check(value == 0 or low <= value <= high, f"{where}: must be 0 (not paced) or in [{low:g}, {high:g}]")
            source, _sep, form = key.partition(":")
            src = c.sources.get(source)
            _check(src is not None, f"{where}: no source named {source!r}")
            _check(src.kind in PACED_KINDS, f"{where}: only {', '.join(PACED_KINDS)} sources can be paced")
            if form:
                _check(src.kind == "filing", f"{where}: only an EDGAR source has forms")
                # a default line for a form this install does not poll is simply unused; a line the
                # owner adds must name a polled form (a misspelt form is an error, never ignored)
                _check(form in c.edgar_atom_forms or key in defaults,
                       f"{where}: {form!r} is not one of edgar_atom_forms")
