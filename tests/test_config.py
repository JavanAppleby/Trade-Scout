"""Config loader (SCOUT_SPEC 13, DESIGN 6): strict keys, types, defaults, validation, secrecy."""

from __future__ import annotations

import re
import tomllib
from datetime import date, time
from pathlib import Path

import pytest
from helpers_wire import TEST_WIRE

from scout.config import (
    CLASS_KEYWORD_KEYS, CONFIG_KEYS, SCALAR_KEYS, ConfigError, ScoutConfig, config_from_dict, load_config,
)

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "config.example.toml"
SECRET = "owner.private@example.invalid"


def test_defaults_match_design(scout_dir: Path) -> None:
    c = config_from_dict({}, scout_dir)
    assert c.contact_email == "" and not c.http_allowed() and c.user_agent() == ""
    assert c.source_max_gap_s == 180 and c.scout_stale_s == 120 and c.jason_near_level_pct == 0.01
    assert c.active_hours_et == (time(4, 0), time(20, 0))
    assert c.fomc_dates[0] == date(2026, 1, 28) and len(c.fomc_dates) == 8
    assert c.llm_enabled is False and c.scout_webull_enabled is False
    # GlobeNewswire was removed on 2026-11-03: it refuses automated readers (403 from Akamai)
    assert set(c.sources) == {"edgar_atom", "edgar_submissions", "edgar_tickers", "prnewswire", "stocktitan",
                              "nasdaq_halts", "nowflation", "bls", "cboe_vix", "bls_data"}
    assert c.source("prnewswire").terms_ok is False
    st = c.source("stocktitan")  # owner, 2026-11-04
    assert (st.kind, st.url, st.allowed_hosts) == ("wire", "https://www.stocktitan.net/rss", ("www.stocktitan.net",))
    assert st.gate6 is True and st.terms_ok is False and st.max_items == 100
    assert c.host_min_spacing_s["www.stocktitan.net"] == 30.0 and c.wire_title_ticker_suffixes == ("Stock News",)
    assert "stocktitan.net" not in c.llm_allowed_domains  # its terms allow the RSS feed, not page reading
    assert "globenewswire.com" not in c.llm_allowed_domains
    assert c.source("cboe_vix").url.startswith("https://cdn-api.cboe.com/")
    assert c.source("cboe_vix").allowed_hosts == ("cdn-api.cboe.com",)
    assert c.source("edgar_atom").max_items == 100 and c.source("edgar_atom").gate6 is True
    assert set(c.class_keywords) == set(CLASS_KEYWORD_KEYS)
    assert c.llm_job("J1").model == "claude-haiku-5-5" and c.llm_job("J4").max_tokens == 3000
    assert c.price("claude-sonnet-5-5").output_per_mtok == 10.0 and c.price("no-such-model") is None
    assert c.web_search_usd_per_1000 == 10.0
    assert [s.name for s in c.enabled_sources("wire")] == ["prnewswire", "stocktitan"]


def test_example_loads_to_the_defaults() -> None:
    example = load_config(EXAMPLE)
    defaults = ScoutConfig(scout_dir=REPO)
    assert example.public_dict() == defaults.public_dict()


def test_example_documents_every_key() -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert set(data) == set(CONFIG_KEYS)  # every key is present in the example
    for key in SCALAR_KEYS:
        line = next(ln for ln in text.splitlines() if re.match(rf"{key}\s*=", ln))
        assert "#" in line, f"{key} has no comment in config.example.toml"
    for name, table in data["sources"].items():
        assert set(table) == {"enabled", "kind", "url", "paths", "min_spacing_s", "idle_spacing_s", "allowed_hosts",
                              "terms_note", "max_items", "gate6", "terms_ok"}, name


def test_with_contact_email(scout_dir: Path) -> None:
    c = config_from_dict({"contact_email": SECRET}, scout_dir)
    assert c.http_allowed()
    assert c.user_agent() == f"Scout/0.2.2 ({SECRET})"
    assert SECRET not in repr(c) and SECRET not in str(c)
    assert SECRET not in str(c.public_dict())


@pytest.mark.parametrize("data, needle", [
    ({"no_such_key": 1}, "no_such_key"),
    ({"sources": {"edgar_atom": {"enabled": True, "bogus": 1}}}, "sources.edgar_atom.bogus"),
    ({"llm": {"jobs": {"J2": {}}}}, "llm.jobs.J2"),
    ({"llm": {"jobs": {"J1": {"temperature": 1}}}}, "llm.jobs.J1.temperature"),
    ({"llm": {"other": {}}}, "llm.other"),
    ({"class_keywords": {k: [] for k in CLASS_KEYWORD_KEYS} | {"other": []}}, "class_keywords.other"),
])
def test_unknown_keys_are_errors(scout_dir: Path, data: dict, needle: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(needle)):
        config_from_dict(data, scout_dir)


@pytest.mark.parametrize("data, needle", [
    ({"source_max_gap_s": "180"}, "source_max_gap_s"),
    ({"source_max_gap_s": 180.5}, "source_max_gap_s"),
    ({"llm_enabled": 1}, "llm_enabled"),
    ({"scan_max_price": True}, "scan_max_price"),
    ({"brief_time_et": "8:20"}, "brief_time_et"),
    ({"brief_time_et": "24:00"}, "brief_time_et"),
    ({"fomc_dates": ["2026-13-01"]}, "fomc_dates"),
    ({"watch_symbols": "ABCD"}, "watch_symbols"),
    ({"watch_symbols": ["abcd"]}, "watch_symbols"),
    ({"radar_dir": {"a": 1}}, "radar_dir"),
    ({"sources": {"edgar_atom": {"min_spacing_s": "60"}}}, "sources.edgar_atom.min_spacing_s"),
    ({"host_min_spacing_s": 0.5}, "host_min_spacing_s"),
])
def test_wrong_types_name_the_key(scout_dir: Path, data: dict, needle: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(needle)):
        config_from_dict(data, scout_dir)


@pytest.mark.parametrize("data, needle", [
    ({"scout_webull_enabled": True}, "scout_webull_enabled"),
    ({"hold_catalyst_classes": ["fda", "rumour"]}, "hold_catalyst_classes"),
    ({"class_keywords": {"fda": ["x"]}}, "class_keywords"),
    ({"sources": {"nasdaq_halts": {"min_spacing_s": 30.0}}}, "sources.nasdaq_halts.min_spacing_s"),
    ({"sources": {"prnewswire": {"min_spacing_s": 10.0}}}, "sources.prnewswire.min_spacing_s"),
    ({"sources": {"prnewswire": {"url": "http://www.prnewswire.com/rss"}}}, "sources.prnewswire.url"),
    ({"sources": {"prnewswire": {"kind": "tweets"}}}, "sources.prnewswire.kind"),
    ({"sources": {"newsource": {"enabled": True}}}, "sources.newsource"),
    ({"levels_dir": "../elsewhere"}, "levels_dir"),
    ({"inbox_dir": "C:/elsewhere" if Path("C:/").is_absolute() else "/elsewhere"}, "inbox_dir"),
    ({"radar_dir": "."}, "radar_dir"),
    ({"radar_dir": "state"}, "radar_dir"),
    ({"active_hours_et": ["20:00", "04:00"]}, "active_hours_et"),
    ({"log_level": "LOUD"}, "log_level"),
    ({"verify_sample_weekday": "Monday"}, "verify_sample_weekday"),
    ({"scout_stale_s": 5}, "scout_stale_s"),
    ({"llm_daily_cost_cap_usd": 50.0, "llm_monthly_cost_cap_usd": 20.0}, "llm_daily_cost_cap_usd"),
    ({"llm": {"jobs": {"J1": {"effort": "huge"}}}}, "llm.jobs.J1.effort"),
    ({"llm": {"prices": {"m": {"input_per_mtok": 1.0}}}}, "llm.prices.m"),
    ({"scout_user_agent": "Scout {token}"}, "scout_user_agent"),
])
def test_validation_names_the_key(scout_dir: Path, data: dict, needle: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(needle)):
        config_from_dict(data, scout_dir)


def test_bad_contact_email_is_not_echoed(scout_dir: Path) -> None:
    bad = "not an email but secret-ish"
    with pytest.raises(ConfigError) as info:
        config_from_dict({"contact_email": bad}, scout_dir)
    assert "contact_email" in str(info.value) and bad not in str(info.value)


def test_user_agent_without_contact_field_refused_when_email_set(scout_dir: Path) -> None:
    with pytest.raises(ConfigError, match="scout_user_agent"):
        config_from_dict({"contact_email": SECRET, "scout_user_agent": "Scout/{version}"}, scout_dir)


def test_partial_source_table_merges_onto_the_default(scout_dir: Path) -> None:
    c = config_from_dict({"sources": {"prnewswire": {"terms_ok": True, "min_spacing_s": 45.0}}}, scout_dir)
    s = c.source("prnewswire")
    assert s.terms_ok is True and s.min_spacing_s == 45.0 and s.allowed_hosts == ("www.prnewswire.com",)


def test_new_source_with_every_key(scout_dir: Path) -> None:
    c = config_from_dict({"sources": {"accesswire": {
        "enabled": False, "kind": "wire", "url": "https://www.accessnewswire.com/rss", "paths": [],
        "min_spacing_s": 60.0, "idle_spacing_s": 0.0, "allowed_hosts": ["www.accessnewswire.com"],
        "terms_note": "phase 2", "max_items": 50, "gate6": False, "terms_ok": False}}}, scout_dir)
    assert c.source("accesswire").enabled is False
    assert "accesswire" not in c.reader_params()["gate6_sources"]


def test_price_table_override_and_addition(scout_dir: Path) -> None:
    c = config_from_dict({"llm": {"prices": {"web_search_usd_per_1000": 12.0, "claude-x": {
        "input_per_mtok": 1.0, "output_per_mtok": 2.0, "cache_write_per_mtok": 1.25, "cache_read_per_mtok": 0.1}}}},
        scout_dir)
    assert c.web_search_usd_per_1000 == 12.0
    assert c.price("claude-x").output_per_mtok == 2.0 and c.price("claude-haiku-5-5") is not None


def test_an_old_globenewswire_table_is_refused(scout_dir: Path) -> None:
    """GlobeNewswire was removed on 2026-11-03: an old config.toml that still turns it off or on
    stops Scout with a clear error rather than building a half-made wire."""
    with pytest.raises(ConfigError, match=re.escape("sources.globenewswire: a new source must give every key")):
        config_from_dict({"sources": {"globenewswire": {"enabled": False}}}, scout_dir)


def test_reader_params(scout_dir: Path) -> None:
    p = config_from_dict({}, scout_dir).reader_params()
    assert p["gate6_sources"] == ["edgar_atom", "prnewswire", "stocktitan"]
    assert set(p["gate6_feeds"]) == {"edgar_atom", "prnewswire", "stocktitan"}
    on = config_from_dict({"sources": TEST_WIRE}, scout_dir).reader_params()  # a second gate-6 wire
    assert on["gate6_sources"] == ["edgar_atom", "prnewswire", "stocktitan", "testwire"]
    assert on["gate6_feeds"]["testwire"] == ["rss"]
    assert p["fomc_decision_time_et"] == "14:00" and p["fomc_dates"][0] == "2026-01-28"
    assert set(p) == {"hold_catalyst_classes", "earnings_lookback_days", "earnings_due_start_days",
                      "earnings_due_end_days_quarter", "earnings_due_end_days_annual", "hold_block_macro_events",
                      "scout_stale_s", "source_max_gap_s", "source_overlap_max_gap_s", "jason_near_level_pct",
                      "jason_market_symbols", "gate6_sources", "gate6_feeds", "fomc_dates",
                      "fomc_decision_time_et", "discord_gate5_enabled"}
    assert p["discord_gate5_enabled"] is True and p["source_overlap_max_gap_s"] == 1800
    assert config_from_dict({"discord_enabled": False}, scout_dir).reader_params()["discord_gate5_enabled"] is False
    assert config_from_dict({"discord_gate5_enabled": False}, scout_dir).reader_params()["discord_gate5_enabled"] is False


@pytest.mark.parametrize("data, needle", [
    ({"discord_channels": ["Daily Watchlist"]}, "discord_channels"),
    ({"discord_channels": ["a", "a"]}, "discord_channels"),
    ({"discord_schedule_channels": ["market-edge-chat"]}, "discord_schedule_channels"),
    ({"discord_capture_hours_et": ["17:00", "07:00"]}, "discord_capture_hours_et"),
    ({"discord_expected_every_min": 0}, "discord_expected_every_min"),
    ({"inbox_poll_s": 1}, "inbox_poll_s"),
])
def test_discord_keys_are_checked(scout_dir: Path, data: dict, needle: str) -> None:
    with pytest.raises(ConfigError, match=needle):
        config_from_dict(data, scout_dir)


def test_load_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.toml")
    bad = tmp_path / "config.toml"
    bad.write_text("contact_email = \n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_config(bad)


def test_load_config_tolerates_bom_and_sets_scout_dir(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes("\ufeffwatch_symbols = [\"ABCD\"]\n".encode("utf-8"))
    c = load_config(path)
    assert c.watch_symbols == ("ABCD",) and c.scout_dir == tmp_path.resolve()


def test_wire_idle_spacing_must_be_zero_or_at_least_30(tmp_path) -> None:
    """Regression: idle_spacing_s was only checked >= 0, so 5 s polled a wire every 5 s overnight."""
    from scout.config import config_from_dict

    with pytest.raises(ConfigError, match="idle_spacing_s"):
        config_from_dict({"sources": {"prnewswire": {"idle_spacing_s": 5.0}}}, tmp_path)
    assert config_from_dict({"sources": {"prnewswire": {"idle_spacing_s": 0.0}}}, tmp_path)
    assert config_from_dict({"sources": {"prnewswire": {"idle_spacing_s": 30.0}}}, tmp_path)
