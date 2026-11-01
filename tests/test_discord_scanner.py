"""Paper Gains scanner cards (owner 2026-11-01; INBOX 3.1, DESIGN 2.7): checked per line, stored as
`scanner` facts, briefed, described by the reader, and never read by gate 5 or gate 6."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from scout_reader.api import Reader, event_before_next_session, fresh_catalyst, news_headline, scanner_alerts
from scout_reader.scanner import scanner_alerts_from

from scout.brief import build_brief
from scout.clock import ManualClock, iso
from scout.config import ScoutConfig
from scout.discord_ingest import DiscordInbox, cli_discord_check
from scout.discord_parse import SCANNER_TIMEFRAMES, Post, Reject, rules_from, validate_line
from scout.facts import FactWriter
from scout.health import HealthWriter
from scout.paths import ScoutPaths
from scout.raw import RawStore
from scout.store import read_jsonl

from conftest import et_at, make_config
from helpers_answers import BEAT_ONLY_SOURCES, GATE6_SOURCES, due_record, fact, good_world, install_coverage, \
    write_beats
from helpers_discord import copy_scanner_fixtures, line, scanner_card, write_capture
from helpers_fake_anthropic import make_ctx
from helpers_fakes import write_records

NOW = datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)  # Monday 09:00 ET
AS_OF = et_at("2026-11-02", "19:45")
OUTSIDE = {"due_start": "2027-01-10", "due_end": "2027-04-05"}


@pytest.fixture(autouse=True)
def _coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    install_coverage(monkeypatch)


def _check(cfg: ScoutConfig, obj: dict, channel: str = "15m-scanner") -> Post | Reject:
    return validate_line(obj, channel=channel, now=NOW, rules=rules_from(cfg), line_no=1)


def _card(channel: str = "15m-scanner", tf: str = "15m", pattern: str = "Outside Bar", state: str | None = "In Force",
          tickers: list | None = None, **over) -> dict:
    return scanner_card(channel, NOW - timedelta(minutes=10), tf, pattern, state,
                        tickers if tickers is not None else ["ACME", "KAPA"], **over)


def _ingest(cfg: ScoutConfig, paths: ScoutPaths, at: datetime) -> DiscordInbox:
    clock = ManualClock(at)
    inbox = DiscordInbox(cfg, paths, clock, FactWriter(paths, clock, cfg.fact_dedupe_days), RawStore(paths, cfg),
                         HealthWriter(paths))
    inbox.poll(at)
    return inbox


def _facts(paths: ScoutPaths, kind: str | None = None) -> list[dict]:
    out = []
    for p in sorted(paths.facts.glob("*.jsonl")):
        out += [r for r in read_jsonl(p) if kind is None or r.get("kind") == kind]
    return out


# --------------------------------------------------------------------------- the line check


def test_the_scanner_channels_are_configured_by_default(cfg: ScoutConfig) -> None:
    assert set(SCANNER_TIMEFRAMES) == {"15m-scanner", "daily-scanner", "weekly-scanner"}
    assert set(SCANNER_TIMEFRAMES) <= set(cfg.discord_channels)
    assert not set(SCANNER_TIMEFRAMES) & set(cfg.discord_schedule_channels)


def test_a_card_is_accepted_with_its_tickers_normalised(cfg: ScoutConfig) -> None:
    got = _check(cfg, _card(pattern="  Inside   Bar ", state=None, tickers=["$muon", "BRK.B", "ACME", "ACME"]))
    assert isinstance(got, Post) and got.kind == "scanner_alert"
    assert got.scanner is not None and got.scanner.to_dict() == {
        "timeframe": "15m", "pattern": "Inside Bar", "state": None, "tickers": ["MUON", "BRK-B", "ACME"]}
    assert got.tickers == ("MUON", "BRK-B", "ACME")
    for channel, tf in SCANNER_TIMEFRAMES.items():
        assert isinstance(_check(cfg, _card(channel, tf) | {"channel": channel}, channel), Post)


@pytest.mark.parametrize("over, channel, reason", [
    ({"channel": "open-outcrier"}, "open-outcrier", "scanner_alert_not_allowed_in_channel"),
    ({"scanner": None}, "15m-scanner", "scanner_required_for_scanner_alert"),
    ({"kind": "post"}, "15m-scanner", "scanner_not_allowed_for_kind"),
    ({"earnings": [{"date": "2026-11-02", "session": "pre", "tickers": ["ACME"]}]}, "15m-scanner",
     "earnings_not_allowed_for_kind"),
])
def test_scanner_kind_and_key_rules(cfg: ScoutConfig, over: dict, channel: str, reason: str) -> None:
    got = _check(cfg, _card() | over, channel)
    assert isinstance(got, Reject) and got.reason == reason


@pytest.mark.parametrize("scanner, reason", [
    ({"timeframe": "daily"}, "scanner:timeframe_not_channel"),
    ({"timeframe": "1h"}, "scanner:timeframe_not_15m_daily_weekly"),
    ({"pattern": ""}, "scanner:pattern_not_a_short_string"),
    ({"pattern": "x" * 81}, "scanner:pattern_not_a_short_string"),
    ({"pattern": 3}, "scanner:pattern_not_a_short_string"),
    ({"state": ""}, "scanner:state_not_a_short_string_or_null"),
    ({"state": ["In Force"]}, "scanner:state_not_a_short_string_or_null"),
    ({"tickers": []}, "scanner:tickers_not_a_non_empty_list"),
    ({"tickers": "ACME"}, "scanner:tickers_not_a_non_empty_list"),
    ({"tickers": ["ACME", "not a ticker"]}, "scanner:bad_ticker"),
    ({"tickers": ["ACME", 7]}, "scanner:bad_ticker"),
    ({"pattern": "Outside \udcff"}, "text_not_utf8"),
    ({"extra": 1}, "scanner:keys_must_be_timeframe_pattern_state_tickers"),
])
def test_the_scanner_object_is_checked(cfg: ScoutConfig, scanner: dict, reason: str) -> None:
    card = _card()
    card["scanner"] = card["scanner"] | scanner
    got = _check(cfg, card)
    assert isinstance(got, Reject) and got.reason == reason


def test_a_plain_post_is_still_allowed_in_a_scanner_channel(cfg: ScoutConfig) -> None:
    got = _check(cfg, line("15m-scanner", NOW - timedelta(minutes=5), "Scanner maintenance tonight (invented)"))
    assert isinstance(got, Post) and got.scanner is None


# --------------------------------------------------------------------------- ingest


def test_fixture_cards_become_scanner_facts(cfg: ScoutConfig, paths: ScoutPaths) -> None:
    copy_scanner_fixtures(paths)
    _ingest(cfg, paths, NOW)
    facts = _facts(paths)
    assert {f["kind"] for f in facts} == {"scanner"}  # no `discord` facts for a card
    got = sorted((f["feed"], f["ticker"], f["extra"]["timeframe"], f["extra"]["pattern"], f["extra"]["state"])
                 for f in facts)
    assert got == [
        ("15m-scanner", "ACME", "15m", "Outside Bar", "In Force"), ("15m-scanner", "BRK-B", "15m", "Inside Bar", None),
        ("15m-scanner", "KAPA", "15m", "Outside Bar", "In Force"), ("15m-scanner", "LAMB", "15m", "Outside Bar", "In Force"),
        ("15m-scanner", "MUON", "15m", "Inside Bar", None), ("15m-scanner", "NUON", "15m", "Failed 2D", None),
        ("15m-scanner", "ZETA", "15m", "Inside Bar", None),
        ("daily-scanner", "ACME", "daily", "Outside Bar", "Current Candle"),
        ("daily-scanner", "OMEG", "daily", "Outside Bar", "Current Candle"),
        ("weekly-scanner", "KAPA", "weekly", "Outside Bar", "Current Candle")]
    acme = next(f for f in facts if f["feed"] == "15m-scanner" and f["ticker"] == "ACME")
    assert acme["source"] == "discord:15m-scanner" and acme["class"] == "scanner_alert"
    assert acme["first_seen_at"] == iso(NOW) and acme["published_at"] == "2026-11-02T13:45:00Z"
    assert acme["url"].endswith(acme["guid"]) and acme["event_kind"] is None
    assert acme["title"] == "ACME: Outside Bar - 15m (In Force)"
    assert acme["extra"]["text"].startswith("Outside Bar - 15m (In Force)\nTickers\nACME")
    rejects = read_jsonl(paths.discord / "rejects" / "2026-11-02.jsonl")
    assert [(r["channel"], r["line"], r["reason"]) for r in rejects] == [
        ("weekly-scanner", 2, "scanner:timeframe_not_channel")]


def test_cards_are_deduped_across_restarts(cfg: ScoutConfig, paths: ScoutPaths) -> None:
    card = _card()
    write_capture(paths, "15m-scanner", NOW - timedelta(minutes=5), [card])
    _ingest(cfg, paths, NOW)
    later = NOW + timedelta(minutes=30)
    write_capture(paths, "15m-scanner", later, [card, _card(tickers=["LAMB"], worker=3)])
    inbox = _ingest(cfg, paths, later)
    assert sorted(f["ticker"] for f in _facts(paths, "scanner")) == ["ACME", "KAPA", "LAMB"]
    assert read_jsonl(paths.discord / "files.jsonl")[-1]["posts_repeat"] == 1
    assert inbox.poll(later) == 0


def test_the_check_command_accepts_the_fixture_and_names_the_bad_line(
        cfg: ScoutConfig, paths: ScoutPaths, capsys: pytest.CaptureFixture[str]) -> None:
    copy_scanner_fixtures(paths)
    ctx = make_ctx(cfg, paths, ManualClock(NOW))
    good = paths.inbox_discord / "15m-scanner" / "20261102T135000Z.jsonl"
    assert cli_discord_check(ctx, argparse.Namespace(file=str(good))) == 0
    bad = paths.inbox_discord / "weekly-scanner" / "20261102T133000Z.jsonl"
    assert cli_discord_check(ctx, argparse.Namespace(file=str(bad))) == 2
    assert "line 2: scanner:timeframe_not_channel" in capsys.readouterr().out
    assert _facts(paths) == []


# --------------------------------------------------------------------------- never a gate input


def _world(scout_dir: Path, cfg: ScoutConfig, symbols: tuple[str, ...], *, discord_ok: bool = False) -> None:
    good_world(scout_dir, cfg, AS_OF)
    write_records(scout_dir, "due", [due_record(s, AS_OF - timedelta(minutes=1), **OUTSIDE) for s in symbols])
    if discord_ok:
        statuses = {s: "ok" for s in (*GATE6_SOURCES, *BEAT_ONLY_SOURCES, "discord_inbox")}
        write_beats(scout_dir, AS_OF - timedelta(seconds=20), AS_OF, every=10, sources=statuses)


def _scanner_fact(symbol: str, seen: datetime, *, pattern: str = "Outside Bar", tf: str = "15m",
                  feed: str = "15m-scanner", guid: str = "1566810361036800009") -> dict:
    return fact(symbol, published=seen - timedelta(minutes=3), seen=seen, kind="scanner", cls="scanner_alert",
                title=f"{symbol}: {pattern} - {tf}", source=f"discord:{feed}", guid=guid,
                extra={"post_kind": "scanner_alert", "timeframe": tf, "pattern": pattern, "state": "In Force",
                       "tickers": [symbol]})


def _answers(scout_dir: Path, symbol: str) -> tuple[dict, dict, dict]:
    return (event_before_next_session(scout_dir, symbol, AS_OF), fresh_catalyst(scout_dir, symbol, AS_OF),
            news_headline(scout_dir, symbol, AS_OF))


@pytest.mark.parametrize("enabled", [True, False])
def test_scanner_facts_never_change_gate5_or_gate6(scout_dir: Path, enabled: bool) -> None:
    cfg = make_config(scout_dir, discord_gate5_enabled=enabled)
    _world(scout_dir, cfg, ("ACME", "BETA"))
    # BETA has a wire catalyst, so both gates have something to say; ACME has nothing
    seen = AS_OF - timedelta(hours=3)
    write_records(scout_dir, "facts", [fact("BETA", published=seen - timedelta(minutes=2), seen=seen, cls="fda")])
    base = {s: _answers(scout_dir, s) for s in ("ACME", "BETA")}
    assert base["ACME"][0]["value"] == "false" and base["ACME"][1]["value"] == "false"
    assert base["BETA"][1]["value"] == "true"
    # scanner cards for both, inside both windows, and one whose pattern text reads like an earnings date
    write_records(scout_dir, "facts", [
        _scanner_fact("ACME", AS_OF - timedelta(hours=1)),
        _scanner_fact("BETA", AS_OF - timedelta(hours=1), guid="1566810612695040009"),
        _scanner_fact("ACME", AS_OF - timedelta(minutes=30), pattern="earnings on November 3", tf="daily",
                      feed="daily-scanner", guid="1566810864353280009")])
    assert {s: _answers(scout_dir, s) for s in ("ACME", "BETA")} == base


def test_ingested_fixture_cards_change_no_gate(scout_dir: Path, cfg: ScoutConfig, paths: ScoutPaths) -> None:
    _world(scout_dir, cfg, ("ACME", "KAPA", "ZETA"))
    base = {s: _answers(scout_dir, s) for s in ("ACME", "KAPA", "ZETA")}
    copy_scanner_fixtures(paths)
    _ingest(cfg, paths, NOW)
    assert _facts(paths, "scanner")
    assert {s: _answers(scout_dir, s) for s in ("ACME", "KAPA", "ZETA")} == base


# --------------------------------------------------------------------------- the reader


def test_reader_lists_the_alerts_for_a_symbol(scout_dir: Path, cfg: ScoutConfig, paths: ScoutPaths) -> None:
    copy_scanner_fixtures(paths)
    _ingest(cfg, paths, NOW)
    _world(scout_dir, cfg, (), discord_ok=True)
    res = scanner_alerts(scout_dir, "acme", AS_OF)
    assert res["value"] == "true" and res["reasons"] == ["scanner_alert"] and not res["stale"]
    assert [(a["timeframe"], a["pattern"], a["state"], a["channel"]) for a in res["alerts"]] == [
        ("15m", "Outside Bar", "In Force", "15m-scanner"), ("daily", "Outside Bar", "Current Candle", "daily-scanner")]
    assert res["evidence"] == [f"discord:{a['fact_id']}" for a in res["alerts"]]
    none = scanner_alerts(scout_dir, "OMEGA", AS_OF)
    assert none["value"] == "false" and none["alerts"] == []
    # the window: a day later, a half-day look-back no longer reaches Monday's cards
    later = AS_OF + timedelta(days=1)
    _world(scout_dir, cfg, ())
    write_beats(scout_dir, later - timedelta(seconds=20), later, every=10,
                sources={s: "ok" for s in (*GATE6_SOURCES, *BEAT_ONLY_SOURCES, "discord_inbox")})
    assert Reader(scout_dir).scanner_alerts("ACME", later, days=0.5)["value"] == "false"
    assert Reader(scout_dir).scanner_alerts("ACME", later)["value"] == "true"


def test_reader_as_of_rule_and_honest_unknown(scout_dir: Path, cfg: ScoutConfig, paths: ScoutPaths) -> None:
    copy_scanner_fixtures(paths)
    _ingest(cfg, paths, AS_OF + timedelta(minutes=5))  # first seen after the as-of: invisible
    _world(scout_dir, cfg, (), discord_ok=True)
    assert scanner_alerts(scout_dir, "ACME", AS_OF)["value"] == "false"
    # with the capture status not ok, "none" is unknown, never "not on a scanner"
    _world(scout_dir, cfg, ())
    stale = {s: "ok" for s in (*GATE6_SOURCES, *BEAT_ONLY_SOURCES)} | {"discord_inbox": "stale"}
    write_beats(scout_dir, AS_OF - timedelta(seconds=5), AS_OF, every=5, sources=stale)
    res = scanner_alerts(scout_dir, "ACME", AS_OF)
    assert res["value"] == "unknown" and res["reasons"] == ["source_not_ok:discord_inbox"]


def test_reader_never_raises(scout_dir: Path) -> None:
    assert scanner_alerts(scout_dir, "ACME", datetime(2026, 11, 2, 12, 0))["reasons"] == ["naive_as_of"]
    assert scanner_alerts(scout_dir, "", AS_OF)["reasons"] == ["bad_symbol"]
    assert scanner_alerts(scout_dir / "missing", "ACME", AS_OF)["value"] == "unknown"
    junk = [None, "x", {"kind": "scanner"}, {"kind": "scanner", "ticker": "ACME", "first_seen_at": iso(NOW),
                                             "published_at": iso(NOW), "extra": {"timeframe": "1h", "pattern": "x"}},
            {"kind": "scanner", "ticker": 5, "first_seen_at": iso(NOW), "published_at": iso(NOW), "extra": {}}]
    res = scanner_alerts_from(junk, {"sources": {"discord_inbox": "ok"}}, "ACME", AS_OF, AS_OF - timedelta(days=7))
    assert res["value"] == "false" and res["alerts"] == []
    assert scanner_alerts_from(None, None, "ACME", AS_OF, AS_OF)["value"] == "unknown"  # type: ignore[arg-type]


# --------------------------------------------------------------------------- the brief


def _radar(tmp_path: Path) -> Path:
    finds = tmp_path / "Radar" / "state" / "finds"
    finds.mkdir(parents=True)
    rows = [{"candidate_id": "c1", "symbol": "ZETA", "found_at": "2026-11-02T13:40:00Z", "price": 42.0}]
    (finds / "2026-11-02.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return tmp_path / "Radar"


def test_the_brief_groups_alerts_by_timeframe_and_pattern(scout_dir: Path, tmp_path: Path) -> None:
    cfg = make_config(scout_dir, radar_dir=str(_radar(tmp_path)), watch_symbols=["KAPA"],
                      discord_scanner_brief_max_tickers=3)
    paths = ScoutPaths.from_config(cfg)
    copy_scanner_fixtures(paths)
    write_capture(paths, "daily-scanner", NOW - timedelta(minutes=2), [
        scanner_card("daily-scanner", NOW - timedelta(minutes=4), "daily", "Inside **[Bar](http://evil.example)**",
                     "<b>x</b>", ["DELT", "EPSI", "GAMA", "OMEG", "ZETA"], worker=13)])
    _ingest(cfg, paths, NOW)
    text = build_brief(make_ctx(cfg, paths, ManualClock(NOW)), date(2026, 11, 2), NOW)
    sec = text.split("Paper Gains scanners since the prior close", 1)[1].split("Capture health", 1)[0]
    lines = [ln for ln in sec.splitlines() if ln.startswith("- ")]
    assert lines[0].startswith("- 15m: Failed 2D: NUON (1 card(s), latest 08:47 ET")
    assert lines[1].startswith("- 15m: Inside Bar: **ZETA** (Radar find), MUON, BRK-B (1 card(s)")
    assert lines[2].startswith("- 15m: Outside Bar (In Force): **KAPA** (watch symbol), ACME, LAMB (1 card(s)")
    # the five-ticker card: flagged first, cut at three, the rest counted; markup escaped
    evil = next(ln for ln in lines if "evil" in ln)
    assert "**ZETA** (Radar find), DELT, EPSI, +2 more" in evil
    assert "](http" not in evil and "<b>" not in evil and "&lt;b&gt;" in evil
    assert any(ln.startswith("- daily: Outside Bar (Current Candle): ACME, OMEG") for ln in lines)
    assert any(ln.startswith("- weekly: Outside Bar (Current Candle): **KAPA** (watch symbol)") for ln in lines)
    assert "- #15m-scanner: last capture Mon 02 Nov" in text


def test_the_brief_says_none_and_respects_the_as_of(scout_dir: Path, cfg: ScoutConfig, paths: ScoutPaths) -> None:
    copy_scanner_fixtures(paths)
    _ingest(cfg, paths, NOW + timedelta(hours=1))  # read after the brief's as-of
    text = build_brief(make_ctx(cfg, paths, ManualClock(NOW)), date(2026, 11, 2), NOW)
    sec = text.split("Paper Gains scanners since the prior close", 1)[1].split("Capture health", 1)[0]
    assert "- none captured" in sec and "KAPA" not in sec
