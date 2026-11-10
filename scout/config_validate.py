"""Checking a built ScoutConfig (DESIGN 6): ranges, cross-key rules, sources, LLM jobs and prices.
Split from `scout.config` to keep each module small; `ScoutConfig.__post_init__` calls `validate`.
Every message names the key; the contact email is never echoed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from scout.config_load import ConfigError
from scout.config_pace import validate_pace
from scout.config_reads import validate_reads
from scout.http_state import hard_cap
from scout.models import FILING_CLASSES, WIRE_CLASSES
from scout.paths import is_within, overlaps

__all__ = ["EFFORTS", "KNOWN_CLASSES", "LOG_LEVELS", "PRICE_KEYS", "SOURCE_KINDS", "WEEKDAYS", "validate"]

SOURCE_KINDS = ("wire", "filing", "reference", "halt", "macro", "vix")
EFFORTS = ("low", "medium", "high", "max")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
KNOWN_CLASSES = frozenset(WIRE_CLASSES) | frozenset(FILING_CLASSES)
PRICE_KEYS = ("input_per_mtok", "output_per_mtok", "cache_write_per_mtok", "cache_read_per_mtok")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_UA_FIELDS = re.compile(r"\{([^{}]*)\}")


def _check(ok: bool, message: str) -> None:
    if not ok:
        raise ConfigError(message)


_RANGES: tuple[tuple[str, float, float], ...] = (
    ("source_max_gap_s", 30, 86400), ("source_overlap_max_gap_s", 0, 86400), ("hour_turn_start_min", 0, 59),
    ("hour_turn_end_min", 0, 59), ("hour_turn_spacing_s", 30, 3600), ("source_backoff_max_min", 1, 1440), ("fetch_max_bytes", 1000, 100_000_000),
    ("fetch_timeout_s", 1, 300), ("http_default_host_spacing_s", 0.1, 3600), ("refused_after_403s", 1, 100),
    ("seen_guid_cap", 100, 1_000_000), ("fact_dedupe_days", 2, 60), ("earnings_lookback_days", 1, 365), ("earnings_due_start_days", 0, 365),
    ("earnings_due_end_days_quarter", 1, 365), ("earnings_due_end_days_annual", 1, 365),
    ("periodic_max_age_days", 30, 3650), ("nt_recent_days", 1, 365), ("scan_max_price", 0.01, 100000),
    ("nowflation_max_age_h", 1, 24 * 30), ("nowflation_max_stale_weight_pct", 0, 100), ("macro_agree_minutes", 0, 1440),
    ("inflation_hot_pp", 0, 100), ("inflation_cooling_pp", 0, 100), ("inflation_regime_window_days", 1, 365),
    ("jobs_trend_months", 1, 12), ("jobs_strong_k", -10000, 10000), ("jobs_weak_k", -10000, 10000),
    ("scout_stale_s", 30, 3600), ("jason_near_level_pct", 0, 1), ("levels_score_min_scored", 1, 100000),
    ("levels_tolerance_atr_mult", 0, 100), ("levels_tolerance_floor_pct", 0, 1), ("levels_null_draws", 1, 100000),
    ("levels_null_window_bars", 1, 100000), ("llm_daily_cost_cap_usd", 0, 1000), ("llm_monthly_cost_cap_usd", 0, 10000),
    ("j1_max_calls_per_day", 0, 100000), ("j3_max_calls_per_day", 0, 10000), ("web_search_max_per_day", 0, 10000),
    ("llm_item_chars", 100, 100000), ("web_fetch_max_content_tokens", 100, 1_000_000), ("web_search_max_uses", 0, 20),
    ("j1_batch_size", 1, 200), ("j1_interval_s", 10, 86400), ("j3_no_fact_after_s", 0, 86400),
    ("llm_timeout_s", 1, 1800), ("web_search_usd_per_1000", 0, 1000), ("verify_days", 1, 365),
    ("verify_sample_size", 1, 1000), ("heartbeat_interval_s", 5, 60), ("loop_interval_s", 0.2, 10),
    ("scout_webull_budget_per_min", 0, 60), ("ticker_map_retry_s", 60, 86400),
    ("levels_long_horizon_sessions", 1, 260), ("inbox_poll_s", 5, 3600), ("discord_text_max_chars", 100, 100000),
    ("discord_image_text_max_chars", 100, 100000), ("discord_max_post_age_days", 1, 60),
    ("discord_posted_at_tolerance_s", 0, 3600), ("discord_schedule_max_ahead_days", 1, 60),
    ("discord_max_file_bytes", 1000, 100_000_000), ("discord_expected_every_min", 1, 1440),
    ("discord_brief_max_items", 1, 200), ("discord_scanner_brief_max_tickers", 1, 200),
)
_CHANNEL = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}")
_DISCORD_LISTS = ("discord_schedule_channels", "discord_prefix_ticker_channels", "discord_headline_channels",
                  "discord_catalyst_channels", "discord_reaction_channels", "discord_cadence_channels")


def _validate_discord(c: Any) -> None:
    for name in c.discord_channels:
        _check(bool(_CHANNEL.fullmatch(name)),
               f"discord_channels: {name!r} must be a channel name (lower case letters, digits, - and _)")
    _check(len(set(c.discord_channels)) == len(c.discord_channels), "discord_channels: a channel is listed twice")
    for key in _DISCORD_LISTS:
        extra = [n for n in getattr(c, key) if n not in c.discord_channels]
        _check(not extra, f"{key}: {extra} are not in discord_channels")
    _check(len(c.discord_capture_hours_et) == 2 and c.discord_capture_hours_et[0] < c.discord_capture_hours_et[1],
           "discord_capture_hours_et: must be two times [start, end] with start before end")


def _validate_hour_turn(c: Any) -> None:
    """The faster polling around the hour (owner, 2026-11-04): wires only, never below a host's
    hard minimum spacing in `scout.http`."""
    from scout.http import HARD_MIN_SPACING_S  # noqa: PLC0415 - scout.http imports this package's config

    _check(c.hour_turn_start_min != c.hour_turn_end_min,
           "hour_turn_start_min: must differ from hour_turn_end_min (an empty or whole-hour window)")
    for name in c.hour_turn_sources:
        src = c.sources.get(name)
        _check(src is not None and src.kind == "wire", f"hour_turn_sources: {name!r} is not a wire source")
        for host in src.allowed_hosts:
            hard = HARD_MIN_SPACING_S.get(host.lower(), 0.0)
            _check(c.hour_turn_spacing_s >= hard,
                   f"hour_turn_spacing_s: must be >= {hard:g} (the hard minimum spacing for {host})")


def validate(c: Any) -> None:
    """Raise ConfigError naming the first bad key of a built ScoutConfig."""
    _check(c.scout_webull_enabled is False,
           "scout_webull_enabled: must be false in Scout v1 (no Webull requests, SCOUT_SPEC 2.2)")
    for name, low, high in _RANGES:
        value = getattr(c, name)
        _check(low <= value <= high, f"{name}: must be in [{low}, {high}], got {value}")
    email = c.contact_email
    _check(email == email.strip() and (not email or bool(_EMAIL.fullmatch(email))),
           "contact_email: must be empty or one e-mail address")  # the value is never echoed
    fields_used = set(_UA_FIELDS.findall(c.scout_user_agent))
    _check(fields_used <= {"version", "contact_email"},
           "scout_user_agent: only {version} and {contact_email} may be used")
    _check(not email or "{contact_email}" in c.scout_user_agent,
           "scout_user_agent: must include {contact_email} (the SEC asks for a contact)")
    for name in ("levels_dir", "inbox_dir"):
        rel = getattr(c, name)
        _check(bool(rel.strip()) and not Path(rel).is_absolute()
               and is_within((c.scout_dir / rel).resolve(strict=False), c.scout_dir),
               f"{name}: must be a folder inside Scout's folder")
    sub = Path(c.radar_handoff_subdir)
    _check(bool(c.radar_handoff_subdir.strip()) and not sub.is_absolute() and not sub.drive and not sub.anchor
           and ".." not in sub.parts,
           "radar_handoff_subdir: must be a folder inside Radar's folder (relative, no drive, no '..')")
    for name in ("radar_dir", "pilot_dir"):
        value = getattr(c, name)
        if value.strip():
            _check(not overlaps(c.scout_dir, c.scout_dir / value),
                   f"{name}: must not be Scout's folder, inside it, or contain it")
    _check(len(c.active_hours_et) == 2 and c.active_hours_et[0] < c.active_hours_et[1],
           "active_hours_et: must be two times [start, end] with start before end")
    _check(len(c.answer_times_et) >= 1, "answer_times_et: must list at least one time")
    _check(len(c.nowflation_read_times_et) >= 1, "nowflation_read_times_et: must list at least one time")
    _check(len(c.bls_data_read_times_et) >= 1, "bls_data_read_times_et: must list at least one time")
    _check(c.jobs_weak_k < c.jobs_strong_k, "jobs_weak_k must be below jobs_strong_k")
    bad = [k for k in c.hold_catalyst_classes if k not in KNOWN_CLASSES]
    _check(not bad, f"hold_catalyst_classes: unknown class(es) {bad}; known: {sorted(KNOWN_CLASSES)}")
    _check(c.earnings_due_start_days < c.earnings_due_end_days_quarter <= c.earnings_due_end_days_annual,
           "earnings_due_start_days: must be below earnings_due_end_days_quarter, itself <= _annual")
    _check(c.log_level in LOG_LEVELS, f"log_level: must be one of {LOG_LEVELS}")
    _check(c.verify_sample_weekday in WEEKDAYS, f"verify_sample_weekday: must be one of {WEEKDAYS}")
    _check(c.llm_daily_cost_cap_usd <= c.llm_monthly_cost_cap_usd,
           "llm_daily_cost_cap_usd: must be <= llm_monthly_cost_cap_usd")
    for host, v in c.host_min_spacing_s.items():
        _check(0 <= v <= 86400, f"host_min_spacing_s.{host}: must be in [0, 86400]")
    for host, v in c.host_max_requests_per_day.items():
        _check(1 <= v <= 1_000_000, f"host_max_requests_per_day.{host}: must be in [1, 1000000]")
        hard = hard_cap(host)
        _check(hard is None or v <= hard, f"host_max_requests_per_day.{host}: must be <= {hard} (a hard cap in code)")
    for s in c.sources.values():
        where = f"sources.{s.name}"
        _check(s.kind in SOURCE_KINDS, f"{where}.kind: must be one of {SOURCE_KINDS}")
        _check(s.url.startswith("https://"), f"{where}.url: must start with https://")
        _check(bool(s.allowed_hosts), f"{where}.allowed_hosts: must list at least one host")
        _check(s.min_spacing_s >= 0 and s.idle_spacing_s >= 0, f"{where}: spacings must be >= 0")
        _check(s.max_items >= 0, f"{where}.max_items: must be >= 0")
        if s.kind == "halt":
            _check(s.min_spacing_s >= 60, f"{where}.min_spacing_s: must be >= 60 (Nasdaq Trader asks for it)")
        if s.kind == "wire":
            _check(s.min_spacing_s >= 30, f"{where}.min_spacing_s: must be >= 30")
            _check(s.idle_spacing_s == 0 or s.idle_spacing_s >= 30,
                   f"{where}.idle_spacing_s: must be 0 (the same) or >= 30")
    _validate_hour_turn(c)
    _validate_discord(c)
    validate_pace(c)
    validate_reads(c)
    for j, job in c.llm_jobs.items():
        _check(bool(job.model.strip()), f"llm.jobs.{j}.model: must be set")
        _check(job.effort in EFFORTS, f"llm.jobs.{j}.effort: must be one of {EFFORTS}")
        _check(1 <= job.max_tokens <= 128000, f"llm.jobs.{j}.max_tokens: must be in [1, 128000]")
    for m, p in c.llm_prices.items():
        for k in PRICE_KEYS:
            _check(0 <= getattr(p, k) <= 1000, f"llm.prices.{m}.{k}: must be in [0, 1000]")
