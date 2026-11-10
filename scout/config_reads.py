"""The thesis-read settings (READS_TEST.md 5.5 and 10; owner, 2026-11-10 and 2026-11-11). Split from
`scout.config` to keep each module small.

Top-level keys (`reads_*`) and one `[llm.readers.<id>]` block per reader, in the style of
`[llm.jobs.*]`:

    reads_enabled = false              the whole feature; the owner turns it on (the only line to add)
    reads_poll_s = 5                   how often Radar's packs file and inbox\\reads are read
    reads_max_pack_age_s = 120         a first pack older than this when its turn comes is skipped ("stale")
    reads_workers_per_reader = 3       Claude calls one reader may have in flight at once (its own pool)
    reads_sessions = ["PRE", "CORE", "POST"]
    reads_reread_min_strength = 4      a first read at or above this keeps a thesis alive (rereads)
    reads_reread_max = 12              at most this many rereads per thesis
    reads_news_lookback_h = 24         news and filings first seen in this many hours are shown
    reads_news_max_items = 8
    reads_item_chars = 300             each news title is cut to this many characters
    reads_sample_seed = "reads-v1"     the seed of the sampling hash
    reads_daily_cost_cap_usd = 1.50    reads stop for the ET day at this spend (reads calls only)
    reads_monthly_cost_cap_usd = 20.0  ... and for the month at this one
    radar_packs_subdir = "state/handoff/packs"   Radar's research feed, inside radar_dir

    The market feed for the reads (0.3.2; owner, 2026-11-11; SCOUT_SPEC 19.5):
    reads_market_enabled = true        the switch (it runs only with reads_enabled and the Alpaca key)
    reads_market_symbols = ["SPY", "QQQ", "IWM"]   1 to 6 tickers
    reads_market_poll_s = 60           one poll this often in the PRE, CORE and POST sessions (15 to 600)
    reads_market_max_age_s = 1800      older market data is not shown to a reader (60 to 3600)
    reads_market_timeout_s = 5         each request's timeout (1 to 10; never above fetch_timeout_s)

    [llm.readers.R1]  enabled, model, effort, max_tokens, sample, rereads, daily_cost_cap_usd,
                      monthly_cost_cap_usd (a reader's own caps; 0 = none)

The market feed's fetch source (`market_fetch_source`) is fixed in code and is NOT in `cfg.sources`,
so it never reaches the board, the heartbeat, the health records or params (amendment B of the
build plan): no `[sources.alpaca_market]` table may be written, and no source may list an Alpaca
host other than the market-data host.

A reader's prompt version is fixed in code (`scout.reads_prompt.PROMPT_VERSION`). Changing its
model or effort starts a new reader for the scorer (READS_TEST.md 5.1); the read line records
model, effort and prompt version, so the scorer never pools them.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from scout.config_load import (
    ConfigError, as_bool, as_float, as_int, as_str, as_str_list, as_table, check_keys, is_subfolder,
)

__all__ = ["ALPACA_DOMAIN", "DATA_HOST", "DEFAULT_READERS", "MARKET_SOURCE", "READS_KEYS", "READ_SESSIONS",
           "ReaderConfig", "ReadsConfig", "market_fetch_source", "other_alpaca_host", "reads_from", "validate_reads"]

# The market feed (0.3.2): the one Alpaca host Scout may ask, and its fetch source, fixed in code.
DATA_HOST = "data.alpaca.markets"
ALPACA_DOMAIN = DATA_HOST.partition(".")[2]  # every Alpaca host is under it; written once (tests/test_boundaries)
MARKET_SOURCE = "alpaca_market"


def other_alpaca_host(host: str) -> bool:
    """True for any Alpaca host (the bare domain or one under it) other than the market-data host."""
    h = str(host).strip().lower().rstrip(".")
    return (h == ALPACA_DOMAIN or h.endswith("." + ALPACA_DOMAIN)) and h != DATA_HOST
_MARKET_TICKER = re.compile(r"[A-Z0-9][A-Z0-9.\-]{0,9}")  # scout.llm_prompts.clean_ticker's rule (no import cycle)

READ_SESSIONS = ("PRE", "CORE", "POST")
READER_EFFORTS = ("low", "medium", "high", "max")
_READER_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,15}")
_READER_KEYS = ("enabled", "model", "effort", "max_tokens", "sample", "rereads", "daily_cost_cap_usd",
                "monthly_cost_cap_usd")

# The owner's choice of 11 Nov 2026 (READS_TEST.md 10: "Go with your suggestion on model").
DEFAULT_READERS: dict[str, dict[str, Any]] = {
    "R1": dict(enabled=True, model="claude-haiku-5-5", effort="medium", max_tokens=4000, sample=1.0, rereads=True,
               daily_cost_cap_usd=0.0, monthly_cost_cap_usd=0.0),
    "R2": dict(enabled=True, model="claude-opus-5-5", effort="low", max_tokens=3000, sample=0.15, rereads=False,
               daily_cost_cap_usd=0.75, monthly_cost_cap_usd=12.0),
}


@dataclass(frozen=True)
class ReaderConfig:
    """One `[llm.readers.<id>]` block."""

    reader_id: str
    enabled: bool
    model: str
    effort: str
    max_tokens: int
    sample: float
    rereads: bool
    daily_cost_cap_usd: float  # 0 = none
    monthly_cost_cap_usd: float  # 0 = none


def _reader(rid: str, given: Mapping[str, Any], base: Mapping[str, Any] | None) -> ReaderConfig:
    where = f"llm.readers.{rid}"
    check_keys(where, given, set(_READER_KEYS))
    m = {**(base or {}), **given}
    missing = [k for k in _READER_KEYS if k not in m]
    if missing:
        raise ConfigError(f"{where}: a new reader must give every key; missing {', '.join(missing)}")
    return ReaderConfig(reader_id=rid, enabled=as_bool(f"{where}.enabled", m["enabled"]),
                        model=as_str(f"{where}.model", m["model"]), effort=as_str(f"{where}.effort", m["effort"]),
                        max_tokens=as_int(f"{where}.max_tokens", m["max_tokens"]),
                        sample=as_float(f"{where}.sample", m["sample"]),
                        rereads=as_bool(f"{where}.rereads", m["rereads"]),
                        daily_cost_cap_usd=as_float(f"{where}.daily_cost_cap_usd", m["daily_cost_cap_usd"]),
                        monthly_cost_cap_usd=as_float(f"{where}.monthly_cost_cap_usd", m["monthly_cost_cap_usd"]))


def _default_readers() -> Mapping[str, ReaderConfig]:
    return MappingProxyType({r: _reader(r, {}, DEFAULT_READERS[r]) for r in DEFAULT_READERS})


@dataclass(frozen=True)
class ReadsConfig:
    enabled: bool = False
    poll_s: float = 5.0
    max_pack_age_s: float = 120.0
    workers_per_reader: int = 3
    sessions: tuple[str, ...] = READ_SESSIONS
    reread_min_strength: int = 4
    reread_max: int = 12
    news_lookback_h: float = 24.0
    news_max_items: int = 8
    item_chars: int = 300
    sample_seed: str = "reads-v1"
    daily_cost_cap_usd: float = 1.50
    monthly_cost_cap_usd: float = 20.0
    packs_subdir: str = "state/handoff/packs"
    readers: Mapping[str, ReaderConfig] = field(default_factory=_default_readers)
    market_enabled: bool = True
    market_symbols: tuple[str, ...] = ("SPY", "QQQ", "IWM")
    market_poll_s: float = 60.0
    market_max_age_s: float = 1800.0  # the delayed whole-tape price is 20 to 35 minutes old by construction
    market_timeout_s: float = 5.0

    def enabled_readers(self) -> tuple[ReaderConfig, ...]:
        return tuple(r for r in self.readers.values() if r.enabled)


# config key -> (ReadsConfig field, converter)
_SCALARS: dict[str, tuple[str, Any]] = {
    "reads_enabled": ("enabled", as_bool), "reads_poll_s": ("poll_s", as_float),
    "reads_max_pack_age_s": ("max_pack_age_s", as_float),
    "reads_workers_per_reader": ("workers_per_reader", as_int),
    "reads_sessions": ("sessions", lambda n, v: tuple(s.strip().upper() for s in as_str_list(n, v))),
    "reads_reread_min_strength": ("reread_min_strength", as_int), "reads_reread_max": ("reread_max", as_int),
    "reads_news_lookback_h": ("news_lookback_h", as_float), "reads_news_max_items": ("news_max_items", as_int),
    "reads_item_chars": ("item_chars", as_int), "reads_sample_seed": ("sample_seed", as_str),
    "reads_daily_cost_cap_usd": ("daily_cost_cap_usd", as_float),
    "reads_monthly_cost_cap_usd": ("monthly_cost_cap_usd", as_float),
    "radar_packs_subdir": ("packs_subdir", as_str),
    "reads_market_enabled": ("market_enabled", as_bool),
    "reads_market_symbols": ("market_symbols", lambda n, v: tuple(s.strip() for s in as_str_list(n, v))),
    "reads_market_poll_s": ("market_poll_s", as_float), "reads_market_max_age_s": ("market_max_age_s", as_float),
    "reads_market_timeout_s": ("market_timeout_s", as_float),
}
READS_KEYS = frozenset(_SCALARS)


def reads_from(data: Mapping[str, Any], readers_table: Any) -> ReadsConfig:
    """The ReadsConfig from the top-level `reads_*` keys of `data` and the `[llm.readers]` table."""
    kwargs: dict[str, Any] = {}
    for key, (name, conv) in _SCALARS.items():
        if key in data:
            if isinstance(data[key], Mapping):
                raise ConfigError(f"{key}: a table is not allowed here; expected a value")
            kwargs[name] = conv(key, data[key])
    given = as_table("llm.readers", readers_table if readers_table is not None else {})
    readers = {r: _reader(r, {}, DEFAULT_READERS[r]) for r in DEFAULT_READERS}
    for rid, table in given.items():
        readers[rid] = _reader(rid, as_table(f"llm.readers.{rid}", table), DEFAULT_READERS.get(rid))
    kwargs["readers"] = MappingProxyType(readers)
    return ReadsConfig(**kwargs)


def _check(ok: bool, message: str) -> None:
    if not ok:
        raise ConfigError(message)


def validate_reads(c: Any) -> None:
    """Raise ConfigError naming the first bad reads key of a built ScoutConfig."""
    r: ReadsConfig = c.reads
    for key, (name, _conv) in _SCALARS.items():
        value = getattr(r, name)
        bounds = _RANGES.get(name)
        if bounds is not None:
            _check(bounds[0] <= value <= bounds[1], f"{key}: must be in [{bounds[0]}, {bounds[1]}], got {value}")
    _check(bool(r.sessions) and all(s in READ_SESSIONS for s in r.sessions),
           f"reads_sessions: must list some of {list(READ_SESSIONS)}")
    _check(bool(r.sample_seed.strip()), "reads_sample_seed: must be set")
    _check(r.daily_cost_cap_usd <= r.monthly_cost_cap_usd,
           "reads_daily_cost_cap_usd: must be <= reads_monthly_cost_cap_usd")
    _check(is_subfolder(r.packs_subdir),
           "radar_packs_subdir: must be a folder inside Radar's folder (relative, no drive, no '..')")
    for rid, rd in r.readers.items():
        where = f"llm.readers.{rid}"
        _check(bool(_READER_ID.fullmatch(rid)) and rid.lower() != "owner",
               f"{where}: a reader id is a letter then up to 15 letters, digits, '-' or '_', and not 'owner'")
        _check(bool(rd.model.strip()), f"{where}.model: must be set")
        _check(rd.effort in READER_EFFORTS, f"{where}.effort: must be one of {READER_EFFORTS}")
        _check(1 <= rd.max_tokens <= 128000, f"{where}.max_tokens: must be in [1, 128000]")
        _check(0.0 <= rd.sample <= 1.0, f"{where}.sample: must be in [0, 1]")
        _check(0.0 <= rd.daily_cost_cap_usd <= 1000, f"{where}.daily_cost_cap_usd: must be in [0, 1000]")
        _check(0.0 <= rd.monthly_cost_cap_usd <= 10000, f"{where}.monthly_cost_cap_usd: must be in [0, 10000]")
        if rd.enabled and c.reads.enabled:
            _check(c.price(rd.model) is not None, f"{where}.model: has no [llm.prices] row")
    _validate_market(c)


def _validate_market(c: Any) -> None:
    r: ReadsConfig = c.reads
    syms = r.market_symbols
    _check(1 <= len(syms) <= 6 and len(set(syms)) == len(syms),
           "reads_market_symbols: must list 1 to 6 different tickers")
    for s in syms:
        _check(bool(_MARKET_TICKER.fullmatch(s)), f"reads_market_symbols: {s!r} is not an upper-case ticker")
    _check(r.market_max_age_s >= r.market_poll_s, "reads_market_max_age_s: must be >= reads_market_poll_s")
    _check(MARKET_SOURCE not in c.sources,
           f"sources.{MARKET_SOURCE}: the market feed's source is fixed in code; turn the feed on or off with "
           "reads_market_enabled")
    for s in c.sources.values():
        hosts = [*s.allowed_hosts, _url_host(s.url)]
        for host in (h.strip().lower().rstrip(".") for h in hosts):
            _check(not other_alpaca_host(host),
                   f"sources.{s.name}: {host} is an Alpaca host Scout may never ask (only {DATA_HOST})")


def _url_host(url: str) -> str:
    """A URL's host as `urlsplit` reads it (user info and a port left out), or "" when unreadable."""
    try:
        return urllib.parse.urlsplit(str(url).strip()).hostname or ""
    except ValueError:
        return ""


def market_fetch_source(c: Any) -> Any:
    """The market feed's fetch source (a `scout.config.SourceConfig`), fixed in code except that it is
    enabled only with `reads_market_enabled`. It is not in `cfg.sources`: nothing lists it to the
    other bots (amendment B)."""
    from scout.config import SourceConfig  # noqa: PLC0415 - scout.config imports this module

    return SourceConfig(
        name=MARKET_SOURCE, enabled=bool(c.reads.market_enabled), kind="market",
        url=f"https://{DATA_HOST}/v2/stocks", paths=(), min_spacing_s=60.0, idle_spacing_s=0.0,
        allowed_hosts=(DATA_HOST,),
        terms_note="The owner's free Alpaca market-data key: snapshots and bars only; no order or account endpoint; "
                   "personal use, never republished.", max_items=0, gate6=False, terms_ok=True)


_RANGES: dict[str, tuple[float, float]] = {
    "poll_s": (1, 3600), "max_pack_age_s": (5, 3600), "workers_per_reader": (1, 8),
    "reread_min_strength": (1, 5), "reread_max": (0, 100),
    "news_lookback_h": (0, 720), "news_max_items": (0, 50), "item_chars": (20, 2000),
    "daily_cost_cap_usd": (0, 1000), "monthly_cost_cap_usd": (0, 10000),
    "market_poll_s": (15, 600), "market_max_age_s": (60, 3600), "market_timeout_s": (1, 10),
}
