"""Scout's config.toml -> frozen ScoutConfig (SCOUT_SPEC 13, DESIGN 2.1 and 6).

Every key is optional; the defaults are DESIGN 6's. An unknown key (top level or inside a table),
a wrong type, or a table where a value belongs is a ConfigError naming the key. `contact_email` is
never echoed and `repr` hides it. All tunables live here; there are no per-ticker settings.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import date, time
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable

from scout import __version__
from scout.config_load import (
    ConfigError, as_bool, as_date_list, as_float, as_int, as_str, as_str_list, as_table, as_time, as_time_list,
    check_keys, read_toml,
)
from scout.config_validate import EFFORTS, KNOWN_CLASSES, LOG_LEVELS, PRICE_KEYS, SOURCE_KINDS, WEEKDAYS, validate
from scout.models import WIRE_CLASSES

__all__ = [
    "CLASS_KEYWORD_KEYS", "CONFIG_KEYS", "ConfigError", "LlmJobConfig", "LlmPrice", "ScoutConfig", "SourceConfig",
    "config_from_dict", "load_config",
]

LLM_JOBS = ("J1", "J3", "J4")
CLASS_KEYWORD_KEYS = tuple(c for c in WIRE_CLASSES if c != "other")
ADDED_CLASS_KEYWORDS = ("bankruptcy",)  # may be left out of a [class_keywords] table: the default list stands
_REEXPORTED = (EFFORTS, KNOWN_CLASSES, LOG_LEVELS, SOURCE_KINDS, WEEKDAYS)  # older imports use scout.config


# --------------------------------------------------------------------------- tables


@dataclass(frozen=True)
class SourceConfig:
    """One `[sources.<name>]` table (DESIGN 6.3)."""

    name: str
    enabled: bool
    kind: str
    url: str
    paths: tuple[str, ...]
    min_spacing_s: float
    idle_spacing_s: float
    allowed_hosts: tuple[str, ...]
    terms_note: str
    max_items: int
    gate6: bool
    terms_ok: bool


@dataclass(frozen=True)
class LlmJobConfig:
    enabled: bool
    model: str
    effort: str
    max_tokens: int


@dataclass(frozen=True)
class LlmPrice:
    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float
    cache_read_per_mtok: float


_SOURCE_KEYS = ("enabled", "kind", "url", "paths", "min_spacing_s", "idle_spacing_s", "allowed_hosts",
                "terms_note", "max_items", "gate6", "terms_ok")
_WIRE_TERMS = "Titles, links and times only until the owner reads the terms (terms_ok = false)."

DEFAULT_SOURCES: dict[str, dict[str, Any]] = {
    "edgar_atom": dict(
        enabled=True, kind="filing",
        url=("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}&company=&dateb="
             "&owner=include&start=0&count=100&output=atom"),
        paths=[], min_spacing_s=60.0, idle_spacing_s=600.0, allowed_hosts=["www.sec.gov"],
        terms_note="SEC fair access: declared User-Agent, at most 10 requests a second.", max_items=100,
        gate6=True, terms_ok=True),
    "edgar_submissions": dict(
        enabled=True, kind="reference", url="https://data.sec.gov/submissions/CIK{cik:010d}.json", paths=[],
        min_spacing_s=0.0, idle_spacing_s=0.0, allowed_hosts=["data.sec.gov"],
        terms_note="SEC fair access; read on demand, cached per CIK per ET day.", max_items=0, gate6=False,
        terms_ok=True),
    "edgar_tickers": dict(
        enabled=True, kind="reference", url="https://www.sec.gov/files/company_tickers_exchange.json", paths=[],
        min_spacing_s=86400.0, idle_spacing_s=86400.0, allowed_hosts=["www.sec.gov"],
        terms_note="SEC fair access; once a day.", max_items=0, gate6=False, terms_ok=True),
    "prnewswire": dict(
        enabled=True, kind="wire", url="https://www.prnewswire.com/rss/news-releases-list.rss", paths=[],
        min_spacing_s=60.0, idle_spacing_s=60.0, allowed_hosts=["www.prnewswire.com"], terms_note=_WIRE_TERMS,
        max_items=20, gate6=True, terms_ok=False),
    "stocktitan": dict(  # owner, 2026-11-04: reposts GlobeNewswire, Business Wire and the other wires
        enabled=True, kind="wire", url="https://www.stocktitan.net/rss", paths=[], min_spacing_s=60.0,
        idle_spacing_s=600.0, allowed_hosts=["www.stocktitan.net"],
        terms_note="Its terms name its RSS feeds as an allowed way to collect data; personal use; at most 30 "
                   "requests in 5 minutes.", max_items=100, gate6=True, terms_ok=False),
    "nasdaq_halts": dict(
        enabled=True, kind="halt", url="https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts", paths=[],
        min_spacing_s=60.0, idle_spacing_s=0.0, allowed_hosts=["www.nasdaqtrader.com"],
        terms_note="At most one read a minute; not polled 20:00-04:00 ET except the one dated read.",
        max_items=0, gate6=False, terms_ok=True),
    "nowflation": dict(
        enabled=True, kind="macro", url="https://nowflation.com",
        paths=["/data/release_calendar.json", "/data/realtime.json", "/forecasts/index.json", "/nowflation.ics"],
        min_spacing_s=0.0, idle_spacing_s=0.0, allowed_hosts=["nowflation.com"],
        terms_note="Credit Nowflation with a link; private use; schedule-driven, capped per day.", max_items=0,
        gate6=False, terms_ok=True),
    "bls": dict(
        enabled=True, kind="macro", url="https://www.bls.gov/schedule/news_release/bls.ics", paths=[],
        min_spacing_s=604800.0, idle_spacing_s=604800.0, allowed_hosts=["www.bls.gov"],
        terms_note="Public schedule; weekly. Path UNVERIFIED until build step 2.", max_items=0, gate6=False,
        terms_ok=True),
    "bls_data": dict(  # owner, 2026-11-04: the jobs report and CPI as published
        enabled=True, kind="macro", url="https://api.bls.gov/publicAPI/v1/timeseries/data/", paths=[],
        min_spacing_s=0.0, idle_spacing_s=0.0, allowed_hosts=["api.bls.gov"],
        terms_note="BLS Public Data API v1: public data, no key; at most 25 requests a day (3 a read).",
        max_items=0, gate6=False, terms_ok=True),
    "cboe_vix": dict(
        enabled=True, kind="vix", url="https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
        paths=[], min_spacing_s=86400.0, idle_spacing_s=86400.0, allowed_hosts=["cdn-api.cboe.com"],
        terms_note="Daily close history; once a trading day after 16:30 ET.", max_items=0, gate6=False,
        terms_ok=True),
}

DEFAULT_CLASS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "offering": ("public offering", "pricing", "priced", "registered direct", "at-the-market", "at the market",
                 "ATM offering", "ATM program", "ATM facility", "equity distribution agreement",
                 "equity purchase agreement", "standby equity", "equity line", "securities purchase agreement",
                 "warrants", "warrant", "inducement agreement", "convertible note", "convertible notes",
                 "convertible senior notes", "convertible debenture", "convertible debentures",
                 "convertible preferred", "convertible bond", "convertible bonds", "convertible promissory",
                 "debt exchange", "exchange of notes", "settlement of notes", "private placement"),
    "reverse_split": ("reverse split", "reverse stock split", "share consolidation"),
    "listing_notice": ("deficiency", "delisting", "Nasdaq notice", "notice of noncompliance", "minimum bid price"),
    "bankruptcy": ("Chapter 11", "Chapter 7", "Chapter 15", "bankruptcy", "voluntary petition", "voluntary petitions",
                   "restructuring support agreement", "creditor protection"),
    "fda": ("FDA approval", "FDA approves", "FDA clearance", "510(k) clearance", "PDUFA", "complete response letter",
            "CRL", "breakthrough therapy designation", "breakthrough device designation", "fast track designation"),
    "trial": ("topline", "top-line", "Phase 1", "Phase 2", "Phase 3", "Phase 1/2", "Phase 2/3", "Phase 1b",
              "Phase 1/2a", "Phase 2a", "Phase 2b", "Phase I", "Phase II", "Phase III", "Phase Ib", "Phase IIa",
              "Phase IIb", "Phase I/II", "Phase II/III", "pivotal trial"),
    "contract": ("contract", "award", "awarded", "agreement", "partnership", "purchase order", "order"),
    "earnings_result": ("reports first quarter", "reports second quarter", "reports third quarter",
                        "reports fourth quarter", "reports full year", "reports Q1", "reports Q2", "reports Q3",
                        "reports Q4", "reports fiscal", "financial results for", "reports results"),
    "earnings_date": (  # narrow on purpose: tried first of all (scout.classify)
        "to report ... results", "to report ... earnings", "to announce ... results",
        "to announce ... earnings", "to release ... results", "to release ... earnings",
        "will report ... results", "will report ... earnings", "will announce ... results",
        "will announce ... earnings", "will release ... results", "will release ... earnings",
        "to host ... results", "to host ... earnings", "will host ... results", "will host ... earnings",
        "to hold ... results", "to hold ... earnings", "will hold ... results", "will hold ... earnings",
        "to webcast ... results", "to webcast ... earnings", "will webcast ... results",
        "will webcast ... earnings", "schedules ... results", "schedules ... earnings",
        "timing of ... results", "timing of ... earnings", "date for ... results", "date for ... earnings",
        "date of ... results", "date of ... earnings", "announces conference call ... results",
        "announces conference call ... earnings", "announces ... results conference call",
        "announces ... earnings conference call", "sets ... earnings call for", "sets ... conference call for",
        "notice of ... earnings call", "notice of ... conference call", "conference call date",
        "results release date", "results to be released", "results to be reported", "results to be announced",
        "earnings to be released", "earnings release date",
    ),
}

# Periodic reports: polled so the gate-5 check (scout.verify) sees a company that reports earnings by
# 10-Q or 10-K alone; never a gate-6 class, so their feeds are not gate-6 coverage.
PERIODIC_FORMS = frozenset({"10-Q", "10-K", "20-F", "40-F"})

DEFAULT_HOST_MIN_SPACING: dict[str, float] = {"www.sec.gov": 0.5, "data.sec.gov": 0.5, "www.nasdaqtrader.com": 60.0,
                                              "www.stocktitan.net": 30.0}
DEFAULT_HOST_MAX_PER_DAY: dict[str, int] = {"nowflation.com": 10, "api.bls.gov": 12}
DEFAULT_LLM_JOBS: dict[str, dict[str, Any]] = {
    "J1": dict(enabled=True, model="claude-haiku-5-5", effort="low", max_tokens=4000),
    "J3": dict(enabled=True, model="claude-sonnet-5-5", effort="medium", max_tokens=4000),
    "J4": dict(enabled=True, model="claude-sonnet-5-5", effort="medium", max_tokens=3000),
}
DEFAULT_LLM_PRICES: dict[str, dict[str, float]] = {  # USD per million tokens; checked at build step 5
    "claude-haiku-5-5": dict(input_per_mtok=0.10, output_per_mtok=0.50, cache_write_per_mtok=0.125,
                             cache_read_per_mtok=0.01),
    "claude-sonnet-5-5": dict(input_per_mtok=2.00, output_per_mtok=10.00, cache_write_per_mtok=2.50,
                              cache_read_per_mtok=0.20),
    "claude-opus-5-5": dict(input_per_mtok=4.00, output_per_mtok=20.00, cache_write_per_mtok=5.00,
                            cache_read_per_mtok=0.20),
}
#: The Paper Gains Discord channels the capture tasks read (MARKET NEWS and MAIN STAGE, without the
#: general chat, and the three SCANNERS channels), surveyed 2026-11-01. Names as Discord shows them.
DISCORD_CHANNELS = ("daily-watchlist", "weekly-trade-plan", "free-analysis", "brianna-news", "stockmktnewz",
                    "major-headlines", "earnings-whispers", "market-movers", "open-outcrier", "15m-scanner",
                    "daily-scanner", "weekly-scanner")
DEFAULT_FOMC_2026 = ("2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16",
                     "2026-10-28", "2026-12-09")


def _frozen(d: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(d))


# --------------------------------------------------------------------------- ScoutConfig


@dataclass(frozen=True)
class ScoutConfig:
    """Every Scout tunable (DESIGN 6). Build with load_config() or config_from_dict()."""

    scout_dir: Path = Path(".")

    # 6.1 paths and identity
    radar_dir: str = ""
    radar_handoff_subdir: str = "state/handoff"  # Radar's finds, <trading day>.jsonl (owner, 2026-11-04)
    pilot_dir: str = ""
    levels_dir: str = "state/levels"
    inbox_dir: str = "inbox"
    watch_symbols: tuple[str, ...] = ()
    contact_email: str = ""
    scout_user_agent: str = "Scout/{version} ({contact_email})"

    # 6.2 sources and HTTP
    source_max_gap_s: int = 180
    source_backoff_max_min: int = 30
    fetch_max_bytes: int = 2_000_000
    fetch_timeout_s: float = 20.0
    http_default_host_spacing_s: float = 1.0
    active_hours_et: tuple[time, ...] = (time(4, 0), time(20, 0))
    refused_after_403s: int = 3
    seen_guid_cap: int = 5000
    fact_dedupe_days: int = 7
    ticker_map_retry_s: int = 600
    edgar_atom_forms: tuple[str, ...] = ("8-K", "6-K", "S-1", "S-3", "F-1", "F-3", "424B", "EFFECT", "NT 10-Q",
                                         "NT 10-K", "10-Q", "10-K")
    ticker_exchange_prefixes: tuple[str, ...] = ("NASDAQ", "Nasdaq", "Nasdaq CM", "NasdaqCM", "Nasdaq GM", "NasdaqGM",
                                                 "Nasdaq GS", "NasdaqGS", "NASDAQ Capital Market",
                                                 "Nasdaq Global Market", "Nasdaq Global Select Market", "NYSE",
                                                 "NYSE American", "NYSE MKT", "NYSE Arca", "OTCQB", "OTCQX",
                                                 "OTC Pink", "OTC", "Cboe BZX")
    wire_title_ticker_suffixes: tuple[str, ...] = ("Stock News",)
    host_min_spacing_s: Mapping[str, float] = field(default_factory=lambda: _frozen(DEFAULT_HOST_MIN_SPACING))
    host_max_requests_per_day: Mapping[str, int] = field(default_factory=lambda: _frozen(DEFAULT_HOST_MAX_PER_DAY))
    class_keywords: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: _frozen(DEFAULT_CLASS_KEYWORDS))
    sources: Mapping[str, SourceConfig] = field(default_factory=lambda: _frozen({}))

    # 6.4 answers
    hold_catalyst_classes: tuple[str, ...] = ("fda", "trial", "contract", "earnings_result")  # owner, 2026-11-04
    earnings_lookback_days: int = 45
    earnings_due_start_days: int = 10
    earnings_due_end_days_quarter: int = 50
    earnings_due_end_days_annual: int = 95
    periodic_max_age_days: int = 200
    nt_recent_days: int = 45
    hold_block_macro_events: bool = False
    fomc_dates: tuple[date, ...] = tuple(date.fromisoformat(d) for d in DEFAULT_FOMC_2026)
    fomc_decision_time_et: time = time(14, 0)
    scan_max_price: float = 20.0
    answer_times_et: tuple[time, ...] = (time(19, 30), time(19, 45))
    radar_answers_after_et: time = time(20, 0)
    radar_answers_as_of_et: time = time(19, 45)

    # 6.5 macro
    nowflation_max_age_h: float = 30.0
    nowflation_max_stale_weight_pct: float = 20.0
    nowflation_fresh_after_et: time = time(9, 15)
    nowflation_read_times_et: tuple[time, ...] = (time(8, 20),)
    nowflation_release_day_time_et: time = time(8, 50)
    macro_agree_minutes: int = 5
    inflation_hot_pp: float = 0.25
    inflation_cooling_pp: float = 0.25
    inflation_regime_window_days: int = 20
    vix_read_after_et: time = time(16, 30)
    bls_data_read_times_et: tuple[time, ...] = (time(8, 40),)
    bls_data_release_day_time_et: time = time(9, 15)
    jobs_trend_months: int = 3
    jobs_strong_k: float = 150.0
    jobs_weak_k: float = 50.0

    # 6.6 readers and Jason
    scout_stale_s: int = 120
    jason_near_level_pct: float = 0.01
    jason_market_symbols: tuple[str, ...] = ("SPY", "QQQ")
    levels_long_horizon_sessions: int = 10
    levels_score_min_scored: int = 20
    levels_tolerance_atr_mult: float = 0.5
    levels_tolerance_floor_pct: float = 0.0075
    levels_null_draws: int = 60
    levels_null_window_bars: int = 126
    levels_null_seed: str = "scout-levels-v1"

    # 6.7 LLM
    llm_enabled: bool = False
    llm_daily_cost_cap_usd: float = 1.00
    llm_monthly_cost_cap_usd: float = 20.0
    j1_max_calls_per_day: int = 400
    j3_max_calls_per_day: int = 15
    web_search_max_per_day: int = 30
    llm_allowed_domains: tuple[str, ...] = ("sec.gov", "prnewswire.com", "businesswire.com", "accesswire.com",
                                            "accessnewswire.com", "newsfilecorp.com", "thenewswire.com",
                                            "nasdaq.com", "fda.gov", "clinicaltrials.gov")
    llm_item_chars: int = 2000
    web_fetch_max_content_tokens: int = 8000
    web_search_max_uses: int = 3
    j1_batch_size: int = 20
    j1_interval_s: int = 120
    j3_time_et: time = time(19, 30)
    j3_no_fact_after_s: int = 600
    llm_timeout_s: float = 120.0
    llm_jobs: Mapping[str, LlmJobConfig] = field(default_factory=lambda: _frozen({}))
    llm_prices: Mapping[str, LlmPrice] = field(default_factory=lambda: _frozen({}))
    web_search_usd_per_1000: float = 10.0

    # 6.8 schedule, verification, loop, Webull
    brief_time_et: time = time(8, 20)
    verify_days: int = 20
    verify_time_et: time = time(20, 30)
    verify_sample_size: int = 10
    verify_sample_weekday: str = "Mon"
    heartbeat_interval_s: int = 30
    loop_interval_s: float = 1.0
    log_level: str = "INFO"
    scout_webull_enabled: bool = False
    scout_webull_budget_per_min: int = 2

    # 6.9 the inbox and Paper Gains Discord captures (owner decisions 2026-11-01; DESIGN 2.7)
    inbox_poll_s: int = 60
    discord_enabled: bool = True
    discord_channels: tuple[str, ...] = DISCORD_CHANNELS
    discord_schedule_channels: tuple[str, ...] = ("daily-watchlist", "open-outcrier", "market-movers")
    discord_prefix_ticker_channels: tuple[str, ...] = ("brianna-news",)
    discord_headline_channels: tuple[str, ...] = ("major-headlines", "stockmktnewz")
    discord_catalyst_channels: tuple[str, ...] = ("brianna-news",)
    discord_reaction_channels: tuple[str, ...] = ("earnings-whispers", "open-outcrier")
    discord_gate5_enabled: bool = True
    discord_text_max_chars: int = 2000
    discord_image_text_max_chars: int = 4000
    discord_max_post_age_days: int = 14
    discord_posted_at_tolerance_s: int = 120
    discord_schedule_max_ahead_days: int = 14
    discord_max_file_bytes: int = 2_000_000
    discord_expected_every_min: int = 40
    discord_cadence_channels: tuple[str, ...] = ("brianna-news", "stockmktnewz", "major-headlines", "earnings-whispers",
                                                 "market-movers", "open-outcrier")
    discord_capture_hours_et: tuple[time, ...] = (time(4, 0), time(20, 0))
    discord_brief_max_items: int = 10
    discord_scanner_brief_max_tickers: int = 12

    def __post_init__(self) -> None:
        object.__setattr__(self, "scout_dir", Path(self.scout_dir).resolve(strict=False))
        if not self.sources:
            object.__setattr__(self, "sources", _frozen(
                {n: _source(n, {}, DEFAULT_SOURCES[n]) for n in DEFAULT_SOURCES}))
        if not self.llm_jobs:
            object.__setattr__(self, "llm_jobs", _frozen(
                {n: _llm_job(n, {}, DEFAULT_LLM_JOBS[n]) for n in DEFAULT_LLM_JOBS}))
        if not self.llm_prices:
            object.__setattr__(self, "llm_prices", _frozen(
                {m: _llm_price(m, p) for m, p in DEFAULT_LLM_PRICES.items()}))
        validate(self)

    # ------------------------------------------------------------------ derived

    def source(self, name: str) -> SourceConfig:
        try:
            return self.sources[name]
        except KeyError:
            raise KeyError(f"no source named {name!r}") from None

    def enabled_sources(self, kind: str | None = None) -> tuple[SourceConfig, ...]:
        return tuple(s for s in self.sources.values() if s.enabled and (kind is None or s.kind == kind))

    def llm_job(self, job: str) -> LlmJobConfig:
        return self.llm_jobs[job]

    def price(self, model: str) -> LlmPrice | None:
        return self.llm_prices.get(model)

    def http_allowed(self) -> bool:
        """SS 6.1: an empty contact_email means no HTTP at all."""
        return bool(self.contact_email.strip())

    def user_agent(self) -> str:
        """The declared User-Agent; empty when contact_email is empty. Never log it."""
        if not self.http_allowed():
            return ""
        return self.scout_user_agent.format(version=__version__, contact_email=self.contact_email.strip())

    def reader_params(self) -> dict[str, Any]:
        """What scout_reader needs to answer "as of t" (DESIGN 5.4; app.write_params adds format,
        first_seen_at and config_sha256)."""
        return {
            "hold_catalyst_classes": list(self.hold_catalyst_classes),
            "earnings_lookback_days": self.earnings_lookback_days,
            "earnings_due_start_days": self.earnings_due_start_days,
            "earnings_due_end_days_quarter": self.earnings_due_end_days_quarter,
            "earnings_due_end_days_annual": self.earnings_due_end_days_annual,
            "hold_block_macro_events": self.hold_block_macro_events,
            "scout_stale_s": self.scout_stale_s,
            "source_max_gap_s": self.source_max_gap_s,
            "jason_near_level_pct": self.jason_near_level_pct,
            "jason_market_symbols": list(self.jason_market_symbols),
            "gate6_sources": sorted(s.name for s in self.enabled_sources() if s.gate6),
            # the feeds each gate-6 source must keep polling (scout_reader.coverage `missing_feed`)
            # (periodic forms are polled for the gate-5 check only: the SS 6.4 table never classes them)
            "gate6_feeds": {s.name: ([f for f in self.edgar_atom_forms if f.upper() not in PERIODIC_FORMS]
                                     if s.kind == "filing" else ["rss"])
                            for s in self.enabled_sources() if s.gate6 and s.kind in ("filing", "wire")},
            "fomc_dates": [d.isoformat() for d in self.fomc_dates],
            "fomc_decision_time_et": self.fomc_decision_time_et.strftime("%H:%M"),
            # gate 5 may read Discord earnings-schedule facts, which can only add "true" (DESIGN 2.7)
            "discord_gate5_enabled": bool(self.discord_enabled and self.discord_gate5_enabled),
        }

    def public_dict(self) -> dict[str, Any]:
        """Every value except contact_email, as plain JSON values (for hashing and display)."""
        out: dict[str, Any] = {}
        for f in fields(self):
            if f.name in ("contact_email", "scout_dir"):
                continue
            out[f.name] = _plain(getattr(self, f.name))
        return out

    def __repr__(self) -> str:  # never show the contact email
        shown = ", ".join(f"{f.name}={getattr(self, f.name)!r}" for f in fields(self) if f.name != "contact_email")
        return f"ScoutConfig({shown}, contact_email={'<set>' if self.contact_email else ''!r})"

    __str__ = __repr__


def _plain(value: Any) -> Any:
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (SourceConfig, LlmJobConfig, LlmPrice)):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, (date, Path)):
        return str(value)
    return value


TABLE_KEYS = frozenset({"host_min_spacing_s", "host_max_requests_per_day", "class_keywords", "sources", "llm"})
_DERIVED = frozenset({"scout_dir", "llm_jobs", "llm_prices", "web_search_usd_per_1000"})
SCALAR_KEYS = frozenset(f.name for f in fields(ScoutConfig)) - TABLE_KEYS - _DERIVED
CONFIG_KEYS: frozenset[str] = SCALAR_KEYS | TABLE_KEYS


# --------------------------------------------------------------------------- coercion


def _upper_words(name: str, value: Any) -> tuple[str, ...]:
    words = as_str_list(name, value)
    for w in words:
        if w != w.strip().upper() or " " in w:
            raise ConfigError(f"{name}: items must be UPPERCASE symbols without spaces, got {w!r}")
    return words


_COERCE: dict[str, Callable[[str, Any], Any]] = {
    "watch_symbols": _upper_words,
    "jason_market_symbols": _upper_words,
    "active_hours_et": as_time_list,
    "answer_times_et": as_time_list,
    "nowflation_read_times_et": as_time_list,
    "bls_data_read_times_et": as_time_list,
    "discord_capture_hours_et": as_time_list,
    "fomc_dates": as_date_list,
}


def _coerce(name: str, value: Any, default: Any) -> Any:
    special = _COERCE.get(name)
    if special is not None:
        return special(name, value)
    if isinstance(value, Mapping):
        raise ConfigError(f"{name}: a table is not allowed here; expected a value")
    for kind, conv in ((bool, as_bool), (int, as_int), (float, as_float), (time, as_time), (str, as_str)):
        if isinstance(default, kind):
            return conv(name, value)
    if isinstance(default, tuple):
        return as_str_list(name, value)
    raise ConfigError(f"{name}: unsupported field type")  # pragma: no cover


def _source(name: str, given: Mapping[str, Any], base: Mapping[str, Any] | None) -> SourceConfig:
    where = f"sources.{name}"
    check_keys(where, given, set(_SOURCE_KEYS))
    merged = {**(base or {}), **given}
    missing = [k for k in _SOURCE_KEYS if k not in merged]
    if missing:
        raise ConfigError(f"{where}: a new source must give every key; missing {', '.join(missing)}")
    return SourceConfig(
        name=name, enabled=as_bool(f"{where}.enabled", merged["enabled"]), kind=as_str(f"{where}.kind", merged["kind"]),
        url=as_str(f"{where}.url", merged["url"]), paths=as_str_list(f"{where}.paths", merged["paths"]),
        min_spacing_s=as_float(f"{where}.min_spacing_s", merged["min_spacing_s"]),
        idle_spacing_s=as_float(f"{where}.idle_spacing_s", merged["idle_spacing_s"]),
        allowed_hosts=as_str_list(f"{where}.allowed_hosts", merged["allowed_hosts"]),
        terms_note=as_str(f"{where}.terms_note", merged["terms_note"]),
        max_items=as_int(f"{where}.max_items", merged["max_items"]), gate6=as_bool(f"{where}.gate6", merged["gate6"]),
        terms_ok=as_bool(f"{where}.terms_ok", merged["terms_ok"]))


def _llm_job(name: str, given: Mapping[str, Any], base: Mapping[str, Any]) -> LlmJobConfig:
    where = f"llm.jobs.{name}"
    check_keys(where, given, {"enabled", "model", "effort", "max_tokens"})
    m = {**base, **given}
    return LlmJobConfig(enabled=as_bool(f"{where}.enabled", m["enabled"]), model=as_str(f"{where}.model", m["model"]),
                        effort=as_str(f"{where}.effort", m["effort"]),
                        max_tokens=as_int(f"{where}.max_tokens", m["max_tokens"]))


_PRICE_KEYS = PRICE_KEYS


def _llm_price(model: str, given: Mapping[str, Any]) -> LlmPrice:
    where = f"llm.prices.{model}"
    as_table(where, given)
    check_keys(where, given, set(_PRICE_KEYS))
    missing = [k for k in _PRICE_KEYS if k not in given]
    if missing:
        raise ConfigError(f"{where}: missing {', '.join(missing)}")
    return LlmPrice(**{k: as_float(f"{where}.{k}", given[k]) for k in _PRICE_KEYS})


def _tables(data: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "host_min_spacing_s" in data:
        t = as_table("host_min_spacing_s", data["host_min_spacing_s"])
        out["host_min_spacing_s"] = _frozen({h: as_float(f"host_min_spacing_s.{h}", v) for h, v in t.items()})
    if "host_max_requests_per_day" in data:
        t = as_table("host_max_requests_per_day", data["host_max_requests_per_day"])
        out["host_max_requests_per_day"] = _frozen(
            {h: as_int(f"host_max_requests_per_day.{h}", v) for h, v in t.items()})
    if "class_keywords" in data:
        t = as_table("class_keywords", data["class_keywords"])
        check_keys("class_keywords", t, set(CLASS_KEYWORD_KEYS))
        # a class added after the install's table was written keeps its default list (owner, 2026-11-04)
        missing = [k for k in CLASS_KEYWORD_KEYS if k not in t and k not in ADDED_CLASS_KEYWORDS]
        if missing:
            raise ConfigError(f"class_keywords: every class must be listed; missing {', '.join(missing)}")
        out["class_keywords"] = _frozen({k: as_str_list(f"class_keywords.{k}", t[k]) if k in t
                                         else DEFAULT_CLASS_KEYWORDS[k] for k in CLASS_KEYWORD_KEYS})
    given_sources = as_table("sources", data.get("sources", {}))
    sources = {n: _source(n, {}, DEFAULT_SOURCES[n]) for n in DEFAULT_SOURCES}
    for name, table in given_sources.items():
        sources[name] = _source(name, as_table(f"sources.{name}", table), DEFAULT_SOURCES.get(name))
    out["sources"] = _frozen(sources)
    llm = as_table("llm", data.get("llm", {}))
    check_keys("llm", llm, {"jobs", "prices"})
    jobs_given = as_table("llm.jobs", llm.get("jobs", {}))
    check_keys("llm.jobs", jobs_given, set(LLM_JOBS))
    out["llm_jobs"] = _frozen({j: _llm_job(j, as_table(f"llm.jobs.{j}", jobs_given.get(j, {})), DEFAULT_LLM_JOBS[j])
                               for j in LLM_JOBS})
    prices_given = dict(as_table("llm.prices", llm.get("prices", {})))
    if "web_search_usd_per_1000" in prices_given:
        out["web_search_usd_per_1000"] = as_float("llm.prices.web_search_usd_per_1000",
                                                  prices_given.pop("web_search_usd_per_1000"))
    prices = {m: _llm_price(m, p) for m, p in DEFAULT_LLM_PRICES.items()}
    for model, table in prices_given.items():
        prices[model] = _llm_price(model, table)
    out["llm_prices"] = _frozen(prices)
    return out


# --------------------------------------------------------------------------- validation


# --------------------------------------------------------------------------- loading


def config_from_dict(data: Mapping[str, Any], scout_dir: Path | str) -> ScoutConfig:
    """Build a ScoutConfig from parsed TOML. Unknown keys (anywhere) are errors."""
    check_keys("", data, CONFIG_KEYS)
    defaults = ScoutConfig.__dataclass_fields__
    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        if name in SCALAR_KEYS:
            default = defaults[name].default
            kwargs[name] = _coerce(name, value, default)
    kwargs.update(_tables(data))
    return ScoutConfig(scout_dir=Path(scout_dir), **kwargs)


def load_config(path: Path | str) -> ScoutConfig:
    """Read Scout's config.toml; scout_dir is the file's resolved folder."""
    path = Path(path)
    return config_from_dict(read_toml(path), path.resolve().parent)
