"""The capture-line check (docs/INBOX.md, DESIGN 2.7): strict per line, never the whole file."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from scout.config import ScoutConfig
from scout.discord_parse import (
    Post, Reject, capture_time, parse_capture, post_tickers, rules_from, snowflake_time, split_capture_name,
    validate_line,
)

from helpers_discord import line, schedule, snowflake

NOW = datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)
POSTED = NOW - timedelta(minutes=30)


def _check(cfg: ScoutConfig, rec: object, channel: str = "open-outcrier") -> Post | Reject:
    return validate_line(rec, channel=channel, now=NOW, rules=rules_from(cfg), line_no=1)


def test_a_good_line_is_a_post(cfg: ScoutConfig) -> None:
    got = _check(cfg, line("open-outcrier", POSTED, "$ZETA (-12.0% pre) invented"))
    assert isinstance(got, Post)
    assert got.tickers == ("ZETA",) and got.kind == "post" and got.earnings == ()


def test_snowflake_time_round_trip() -> None:
    at = datetime(2026, 11, 2, 13, 9, 5, tzinfo=timezone.utc)
    assert snowflake_time(snowflake(at)) == at
    assert snowflake_time("12ab") is None and snowflake_time("") is None


@pytest.mark.parametrize("change, reason", [
    (lambda r: r.pop("author"), "missing_key:author"),
    (lambda r: r.update(extra=1), "unknown_key:extra"),
    (lambda r: r.update(format=2), "unknown_format"),
    (lambda r: r.update(format=True), "unknown_format"),
    (lambda r: r.update(channel="brianna-news"), "channel_not_folder"),
    (lambda r: r.update(message_id="abc"), "message_id_not_a_snowflake"),
    (lambda r: r.update(permalink=r["permalink"][:-3] + "000"), "permalink_mismatch"),
    (lambda r: r.update(permalink="https://evil.example/channels/1/2/" + r["message_id"]), "permalink_mismatch"),
    (lambda r: r.update(posted_at="2026-11-02 13:30:00"), "posted_at_not_utc_iso_z"),
    (lambda r: r.update(posted_at="2026-11-02T12:00:00Z"), "posted_at_disagrees_with_message_id"),
    (lambda r: r.update(captured_at="2026-11-02T12:00:00Z"), "captured_at_out_of_range"),
    (lambda r: r.update(captured_at="2026-11-02T15:00:00Z"), "captured_at_out_of_range"),
    (lambda r: r.update(author=""), "author_not_a_short_string"),
    (lambda r: r.update(text=None), "text_not_a_string"),
    (lambda r: r.update(image_text=3), "image_text_not_string_or_null"),
    (lambda r: r.update(kind="chat"), "kind_unknown"),
    (lambda r: r.update(kind="earnings_schedule"), "earnings_required_for_earnings_schedule"),
    (lambda r: r.update(earnings=[{"date": "2026-11-03", "session": "pre", "tickers": ["ACME"]}]),
     "earnings_not_allowed_for_kind"),
])
def test_each_bad_field_is_named(cfg: ScoutConfig, change, reason: str) -> None:
    rec = line("open-outcrier", POSTED)
    change(rec)
    got = _check(cfg, rec)
    assert isinstance(got, Reject) and got.reason == reason


def test_future_and_old_posts_are_refused(cfg: ScoutConfig) -> None:
    future = NOW + timedelta(hours=1)
    got = _check(cfg, line("open-outcrier", future, captured=future))
    assert isinstance(got, Reject) and got.reason == "posted_at_in_future"
    old = NOW - timedelta(days=cfg.discord_max_post_age_days, hours=1)
    got = _check(cfg, line("open-outcrier", old))
    assert isinstance(got, Reject) and got.reason == "too_old"


def test_unconfigured_channel(cfg: ScoutConfig) -> None:
    got = _check(cfg, line("market-edge-chat", POSTED), channel="market-edge-chat")
    assert isinstance(got, Reject) and got.reason == "channel_not_configured"


@pytest.mark.parametrize("entries, reason", [
    ([], "earnings:not_a_non_empty_list"),
    ([{"date": "2026-11-03", "session": "pre"}], "earnings[0]:keys_must_be_date_session_tickers"),
    ([{"date": "11/3", "session": "pre", "tickers": []}], "earnings[0]:date_not_YYYY-MM-DD"),
    ([{"date": "2026-10-30", "session": "pre", "tickers": []}], "earnings[0]:date_out_of_range"),
    ([{"date": "2026-12-30", "session": "pre", "tickers": []}], "earnings[0]:date_out_of_range"),
    ([{"date": "2026-11-03", "session": "am", "tickers": []}], "earnings[0]:session_not_pre_post_unknown"),
    ([{"date": "2026-11-03", "session": "pre", "tickers": ["not a ticker"]}], "earnings[0]:bad_ticker"),
    ([{"date": "2026-11-03", "session": "pre", "tickers": "ACME"}], "earnings[0]:tickers_not_a_list"),
])
def test_earnings_entries_are_checked(cfg: ScoutConfig, entries: list, reason: str) -> None:
    rec = line("open-outcrier", POSTED, kind="earnings_schedule", earnings=entries)
    got = _check(cfg, rec)
    assert isinstance(got, Reject) and got.reason == reason


def test_earnings_only_in_schedule_channels(cfg: ScoutConfig) -> None:
    rec = schedule("earnings-whispers", POSTED, "2026-11-03", "pre", ["ACME"])
    got = _check(cfg, rec, channel="earnings-whispers")
    assert isinstance(got, Reject) and got.reason == "earnings_not_allowed_in_channel"


def test_schedule_entries_normalised(cfg: ScoutConfig) -> None:
    rec = schedule("open-outcrier", POSTED, "2026-11-03", "pre", ["$acme", "BRK.B", "ACME"])
    got = _check(cfg, rec)
    assert isinstance(got, Post)
    assert got.earnings[0].tickers == ("ACME", "BRK-B") and got.earnings[0].session == "pre"
    empty = _check(cfg, schedule("open-outcrier", POSTED, "2026-10-30", "post", []) | {
        "earnings": [{"date": "2026-11-01", "session": "post", "tickers": []}]})
    assert isinstance(empty, Post) and empty.earnings[0].tickers == ()  # "Top Earnings ... Aft: None"


def test_levels_card_may_carry_its_er_lists(cfg: ScoutConfig) -> None:
    rec = line("daily-watchlist", POSTED, "Daily Watchlist: 11/02", kind="levels_card",
               earnings=[{"date": "2026-11-02", "session": "post", "tickers": ["GAMA"]}])
    got = _check(cfg, rec, channel="daily-watchlist")
    assert isinstance(got, Post) and got.earnings[0].tickers == ("GAMA",)


def test_text_and_image_are_cut_to_their_caps(cfg: ScoutConfig) -> None:
    rec = line("open-outcrier", POSTED, "x" * (cfg.discord_text_max_chars + 50),
               image_text="y" * (cfg.discord_image_text_max_chars + 1))
    got = _check(cfg, rec)
    assert isinstance(got, Post)
    assert len(got.text) == cfg.discord_text_max_chars and got.text_truncated
    assert got.image_text is not None and len(got.image_text) == cfg.discord_image_text_max_chars and got.image_truncated


def test_tickers_from_cashtags_prefix_and_tags(cfg: ScoutConfig) -> None:
    rules = rules_from(cfg)
    assert post_tickers("JAGZ - Invented Co Signs $38 Million Deal", "brianna-news", rules) == ["JAGZ"]
    # the prefix form is read only in discord_prefix_ticker_channels: "CPI - ..." names no ticker elsewhere
    assert post_tickers("CPI - hotter than expected", "major-headlines", rules) == []
    assert post_tickers("$TWLZ (-19.5% pre) and $brk.b, $38M", "open-outcrier", rules) == ["TWLZ", "BRK-B"]
    assert post_tickers("Invented Corp (NASDAQ: ACME) wins", "stockmktnewz", rules) == ["ACME"]


def test_a_file_keeps_its_good_lines(cfg: ScoutConfig) -> None:
    good = line("open-outcrier", POSTED)
    other = line("open-outcrier", POSTED + timedelta(seconds=5), "$BETA invented", worker=9)
    body = "\n".join([json.dumps(good), "{not json", json.dumps(dict(good)), "[1, 2]", json.dumps(other)])
    cap = parse_capture(body.encode(), channel="open-outcrier", name="20261102T133000Z.jsonl", now=NOW,
                        rules=rules_from(cfg))
    assert [p.message_id for p in cap.posts] == [good["message_id"], other["message_id"]]  # last line, no newline, whole
    assert [(r.line_no, r.reason) for r in cap.rejects] == [(2, "not_json"), (3, "repeated_in_file"),
                                                            (4, "not_an_object")]
    assert cap.lines == 5


def test_a_partial_last_line_is_refused_and_an_empty_file_is_fine(cfg: ScoutConfig) -> None:
    good = json.dumps(line("open-outcrier", POSTED))
    cap = parse_capture((good + "\n" + good[:40]).encode(), channel="open-outcrier", name="x", now=NOW,
                        rules=rules_from(cfg))
    assert len(cap.posts) == 1 and [r.reason for r in cap.rejects] == ["partial_line"]
    empty = parse_capture(b"", channel="open-outcrier", name="x", now=NOW, rules=rules_from(cfg))
    assert empty.posts == [] and empty.rejects == [] and empty.lines == 0
    bom = parse_capture(b"\xef\xbb\xbf" + good.encode() + b"\n", channel="open-outcrier", name="x", now=NOW,
                        rules=rules_from(cfg))
    assert len(bom.posts) == 1


def test_capture_names() -> None:
    assert split_capture_name("20261102T133000Z.jsonl") == "20261102T133000Z"
    assert split_capture_name("20261102T133000Z.jsonl.tmp") == "20261102T133000Z"
    assert split_capture_name("capture.jsonl") is None
    assert capture_time("20261102T133000Z.jsonl") == datetime(2026, 11, 2, 13, 30, tzinfo=timezone.utc)
    assert capture_time("20261340T133000Z.jsonl") is None


@pytest.mark.parametrize("field", ["author", "text", "image_text"])
def test_a_lone_surrogate_escape_is_refused(cfg: ScoutConfig, field: str) -> None:
    """json.loads keeps "\\udcff" as a lone surrogate, which cannot be stored as UTF-8."""
    rec = line("open-outcrier", POSTED)
    raw = json.dumps(rec).replace('"' + field + '": ' + json.dumps(rec[field]), '"' + field + '": "x \\udcff y"')
    cap = parse_capture((raw + "\n").encode("utf-8"), channel="open-outcrier", name="20261102T140000Z.jsonl",
                        now=NOW, rules=rules_from(cfg))
    assert cap.posts == [] and [r.reason for r in cap.rejects] == ["text_not_utf8"]


def test_reasons_never_carry_raw_key_text(cfg: ScoutConfig) -> None:
    rec = line("open-outcrier", POSTED)
    rec["a\nFAKE LOG LINE " + "z" * 3000] = 1
    got = _check(cfg, rec)
    assert isinstance(got, Reject) and got.reason == "unknown_key:?"
    many = line("open-outcrier", POSTED) | {f"k{n}": n for n in range(6)} | {"bad key!": 1}
    got = _check(cfg, many)
    assert isinstance(got, Reject) and got.reason == "unknown_key:?,k0,k1,+4"
    assert len(got.reason) < 40 and "\n" not in got.reason


def test_a_refused_line_keeps_only_a_snowflake_message_id(cfg: ScoutConfig) -> None:
    got = _check(cfg, line("open-outcrier", POSTED) | {"message_id": "x\n" * 500})
    assert isinstance(got, Reject) and got.reason == "message_id_not_a_snowflake" and got.message_id is None


def test_a_levels_card_with_no_er_lines_may_carry_an_empty_list(cfg: ScoutConfig) -> None:
    card = line("daily-watchlist", POSTED, "Daily Watchlist: 11/02", kind="levels_card", earnings=[])
    got = _check(cfg, card, channel="daily-watchlist")
    assert isinstance(got, Post) and got.earnings == ()
    sched = line("open-outcrier", POSTED, kind="earnings_schedule", earnings=[])
    got = _check(cfg, sched)
    assert isinstance(got, Reject) and got.reason == "earnings:not_a_non_empty_list"
