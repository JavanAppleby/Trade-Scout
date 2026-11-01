"""The Discord inbox job (DESIGN 2.7): capture files in, facts out, never touching the inbox file."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scout.clock import ManualClock, iso
from scout.config import ScoutConfig, config_from_dict
from scout.discord_ingest import (
    DiscordInbox, cadence_status, cli_discord_check, in_window_seconds, last_captures,
)
from scout.facts import FactWriter
from scout.health import HealthWriter
from scout.heartbeat import SourceStatusBoard
from scout.paths import ScoutPaths
from scout.raw import RawStore
from scout.store import read_jsonl

from helpers_discord import copy_fixture_inbox, line, schedule, write_capture

NOW = datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)  # Monday 09:00 ET


def _inbox(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock, board: SourceStatusBoard | None = None):
    return DiscordInbox(cfg, paths, clock, FactWriter(paths, clock, cfg.fact_dedupe_days), RawStore(paths, cfg),
                        HealthWriter(paths), board)


def _facts(paths: ScoutPaths, kind: str | None = None) -> list[dict]:
    out = []
    for p in sorted(paths.facts.glob("*.jsonl")):
        out += [r for r in read_jsonl(p) if kind is None or r.get("kind") == kind]
    return out


def test_fixture_inbox_becomes_facts(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock, write_audit) -> None:
    copy_fixture_inbox(paths)
    before = {p: p.read_bytes() for p in paths.inbox_discord.rglob("*") if p.is_file()}
    board = SourceStatusBoard(cfg)
    with write_audit.watching(paths.root):
        new = _inbox(cfg, paths, clock, board).poll(clock.now())
    assert new == 5  # the bad open-outcrier line is refused
    posts = _facts(paths, "discord")
    by_feed = {(f["feed"], f["ticker"]) for f in posts}
    assert ("brianna-news", "ACME") in by_feed and ("major-headlines", None) in by_feed
    assert ("open-outcrier", "ZETA") in by_feed and ("open-outcrier", "DELT") in by_feed
    acme = next(f for f in posts if f["feed"] == "brianna-news")
    assert acme["source"] == "discord:brianna-news" and acme["class"] == "contract"
    assert acme["url"].endswith(acme["guid"]) and acme["published_at"] == "2026-11-02T13:28:00Z"
    assert acme["first_seen_at"] == iso(clock.now()) and acme["raw_sha256"]
    sched = _facts(paths, "earnings_schedule")
    assert sorted((f["ticker"], f["extra"]["earnings_date"], f["extra"]["session"]) for f in sched) == [
        ("ACME", "2026-11-02", "pre"), ("BETA", "2026-11-02", "pre"), ("DELT", "2026-11-03", "pre"),
        ("EPSI", "2026-11-03", "pre"), ("GAMA", "2026-11-02", "post")]
    assert {f["source"] for f in sched} == {"discord:daily-watchlist", "discord:open-outcrier"}
    # the inbox is never edited, moved or deleted
    assert {p: p.read_bytes() for p in paths.inbox_discord.rglob("*") if p.is_file()} == before
    # raw copies, rejects with reasons, health per file, bookkeeping per file
    assert len(list((paths.raw / "discord").rglob("*.jsonl"))) == 4
    rejects = read_jsonl(paths.discord / "rejects" / "2026-11-02.jsonl")
    assert [(r["channel"], r["line"], r["reason"]) for r in rejects] == [("open-outcrier", 3, "permalink_mismatch")]
    health = [r for r in read_jsonl(paths.health / "2026-11-02.jsonl") if r["source"] == "discord_inbox"]
    assert len(health) == 4 and {r["result"] for r in health} == {"ok", "schema"}
    assert all(r["gate6"] is False for r in health)
    assert len(read_jsonl(paths.discord / "files.jsonl")) == 4
    assert board.sources["discord_inbox"].status == "ok"


def test_repeats_are_skipped_across_restarts(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock) -> None:
    rows = [line("open-outcrier", NOW - timedelta(minutes=20), "$ACME invented")]
    write_capture(paths, "open-outcrier", NOW - timedelta(minutes=10), rows)
    assert _inbox(cfg, paths, clock).poll(clock.now()) == 1
    clock.advance(600)
    # a restart, then a new capture that repeats the same post plus one new one
    write_capture(paths, "open-outcrier", clock.now(), rows + [line("open-outcrier", NOW - timedelta(minutes=5),
                                                                    "$BETA invented", worker=3)])
    again = _inbox(cfg, paths, clock)
    assert again.poll(clock.now()) == 1
    assert again.poll(clock.now()) == 0  # nothing new: the bookkeeping skips both files
    assert sorted(f["ticker"] for f in _facts(paths, "discord")) == ["ACME", "BETA"]
    book = read_jsonl(paths.discord / "files.jsonl")
    assert [b["posts_new"] for b in book] == [1, 1] and book[-1]["posts_repeat"] == 1


def test_a_changed_file_is_read_again(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock) -> None:
    first = line("open-outcrier", NOW - timedelta(minutes=20), "$ACME invented")
    path = write_capture(paths, "open-outcrier", NOW - timedelta(minutes=10), [first])
    inbox = _inbox(cfg, paths, clock)
    assert inbox.poll(clock.now()) == 1
    write_capture(paths, "open-outcrier", NOW, [first, line("open-outcrier", NOW - timedelta(minutes=2),
                                                            "$BETA invented", worker=5)], name=path.name)
    assert inbox.poll(clock.now()) == 1


def test_temp_hidden_and_old_files_are_ignored(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock) -> None:
    folder = paths.inbox_discord / "open-outcrier"
    folder.mkdir(parents=True)
    rec = json.dumps(line("open-outcrier", NOW - timedelta(minutes=5)))
    (folder / "20261102T135500Z.jsonl.tmp").write_text(rec + "\n", encoding="utf-8")
    (folder / ".hidden.jsonl").write_text(rec + "\n", encoding="utf-8")
    (folder / "20261001T120000Z.jsonl").write_text(rec + "\n", encoding="utf-8")  # older than the age limit
    inbox = _inbox(cfg, paths, clock)
    assert inbox.poll(clock.now()) == 0 and _facts(paths) == []
    assert read_jsonl(paths.discord / "files.jsonl") == []


def test_bad_names_and_unknown_channels_are_recorded_once(cfg: ScoutConfig, paths: ScoutPaths,
                                                          clock: ManualClock) -> None:
    rec = line("open-outcrier", NOW - timedelta(minutes=5))
    write_capture(paths, "open-outcrier", NOW, [rec], name="capture.jsonl")
    write_capture(paths, "market-edge-chat", NOW, [rec | {"channel": "market-edge-chat"}])
    inbox = _inbox(cfg, paths, clock)
    assert inbox.poll(clock.now()) == 0
    assert inbox.poll(clock.now()) == 0
    reasons = [r["reason"] for r in read_jsonl(paths.discord / "rejects" / "2026-11-02.jsonl")]
    assert sorted(reasons) == ["bad_file_name", "channel_not_configured"]
    assert _facts(paths) == []


def test_file_too_large_is_refused_whole(scout_dir: Path, clock: ManualClock) -> None:
    cfg = config_from_dict({"contact_email": "", "discord_max_file_bytes": 1000}, scout_dir)
    paths = ScoutPaths.from_config(cfg)
    paths.ensure()
    write_capture(paths, "open-outcrier", NOW, [line("open-outcrier", NOW - timedelta(minutes=5), "x" * 1200)])
    assert _inbox(cfg, paths, clock).poll(clock.now()) == 0
    assert [r["reason"] for r in read_jsonl(paths.discord / "rejects" / "2026-11-02.jsonl")] == ["file_too_large"]


def test_a_partial_last_line_is_refused_not_the_file(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock) -> None:
    good = line("open-outcrier", NOW - timedelta(minutes=5), "$ACME invented")
    write_capture(paths, "open-outcrier", NOW, [good], tail='{"format": 1, "chan')
    assert _inbox(cfg, paths, clock).poll(clock.now()) == 1
    assert [r["reason"] for r in read_jsonl(paths.discord / "rejects" / "2026-11-02.jsonl")] == ["partial_line"]


def test_an_empty_capture_counts_for_the_cadence(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock) -> None:
    board = SourceStatusBoard(cfg)
    inbox = _inbox(cfg, paths, clock, board)
    inbox.poll(clock.now())
    assert board.sources["discord_inbox"].status == "stale"
    assert board.sources["discord_inbox"].last_error == "no_capture_yet"
    write_capture(paths, "open-outcrier", NOW - timedelta(minutes=1), [])
    inbox.poll(clock.now())
    state = board.sources["discord_inbox"]
    assert state.status == "ok" and state.last_ok == "2026-11-02T13:59:00Z"
    # a restart reads the cadence from the bookkeeping
    clock.advance(30 * 60)
    fresh = _inbox(cfg, paths, clock, SourceStatusBoard(cfg))
    assert fresh.publish(clock.now()).status == "ok"
    assert fresh.poll(clock.now()) == 0 and len(read_jsonl(paths.discord / "files.jsonl")) == 1  # not re-read
    assert last_captures(paths)["open-outcrier"]["capture_at"] == "2026-11-02T13:59:00Z"


def test_cadence_counts_capture_hours_on_trading_days_only(cfg: ScoutConfig) -> None:
    # defaults: 07:00-17:00 ET, every 60 minutes
    fri_1650 = datetime(2026, 10, 30, 20, 50, tzinfo=timezone.utc)  # Friday 16:50 ET (EDT)
    mon_0730 = datetime(2026, 11, 2, 12, 30, tzinfo=timezone.utc)  # Monday 07:30 ET (EST)
    assert in_window_seconds(fri_1650, mon_0730, cfg) == pytest.approx(40 * 60)
    assert cadence_status(fri_1650, mon_0730, cfg) == ("ok", None)
    mon_0815 = mon_0730 + timedelta(minutes=45)
    status, why = cadence_status(fri_1650, mon_0815, cfg)
    assert status == "stale" and why == "no_capture_for_85_min_of_capture_hours"
    sat = datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)
    assert cadence_status(fri_1650, sat, cfg) == ("ok", None)
    off = config_from_dict({"discord_enabled": False}, cfg.scout_dir)
    assert cadence_status(None, sat, off) == ("disabled", None)


def test_disabled_reads_nothing(scout_dir: Path, clock: ManualClock) -> None:
    cfg = config_from_dict({"discord_enabled": False}, scout_dir)
    paths = ScoutPaths.from_config(cfg)
    paths.ensure()
    write_capture(paths, "open-outcrier", NOW, [line("open-outcrier", NOW - timedelta(minutes=5))])
    board = SourceStatusBoard(cfg)
    assert _inbox(cfg, paths, clock, board).poll(clock.now()) == 0
    assert board.sources["discord_inbox"].status == "disabled" and _facts(paths) == []


def test_schedule_facts_are_written_before_the_post(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock) -> None:
    write_capture(paths, "open-outcrier", NOW, [schedule("open-outcrier", NOW - timedelta(minutes=3), "2026-11-03",
                                                         "pre", ["DELT"])])
    _inbox(cfg, paths, clock).poll(clock.now())
    kinds = [f["kind"] for f in _facts(paths)]
    assert kinds == ["earnings_schedule", "discord"]
    sched = _facts(paths, "earnings_schedule")[0]
    assert sched["event_kind"] == "earnings" and sched["class"] is None and sched["extra"]["session"] == "pre"


class _Ctx:
    def __init__(self, cfg: ScoutConfig, clock: ManualClock) -> None:
        self.cfg, self.clock, self.log = cfg, clock, logging.getLogger("test.discord")


def test_check_command_writes_nothing(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock, capsys,
                                      write_audit) -> None:
    good = write_capture(paths, "open-outcrier", NOW, [line("open-outcrier", NOW - timedelta(minutes=5))])
    tmp = good.with_name("20261102T140100Z.jsonl.tmp")
    tmp.write_text(json.dumps(line("open-outcrier", NOW - timedelta(minutes=5)) | {"kind": "chat"}) + "\n",
                   encoding="utf-8")
    ctx = _Ctx(cfg, clock)
    with write_audit.watching(paths.root):
        assert cli_discord_check(ctx, argparse.Namespace(file=str(good))) == 0
        assert "OK: 1 post(s) accepted" in capsys.readouterr().out
        assert cli_discord_check(ctx, argparse.Namespace(file=str(tmp))) == 2
        assert "line 1: kind_unknown" in capsys.readouterr().out
        bad = good.with_name("capture.jsonl")
        assert cli_discord_check(ctx, argparse.Namespace(file=str(bad))) == 2
        assert capsys.readouterr().out.startswith("REFUSED: bad_file_name")
    assert read_jsonl(paths.discord / "files.jsonl") == [] and _facts(paths) == []


def test_the_job_is_wired(scout_dir: Path) -> None:
    from scout import cli
    from scout.app import JOB_MODULES

    assert "scout.jobs_discord" in JOB_MODULES
    assert JOB_MODULES.index("scout.jobs_discord") < JOB_MODULES.index("scout.jobs_answers")
    config = scout_dir / "config.toml"
    config.write_text('contact_email = ""\n', encoding="utf-8")
    assert cli.main(["--config", str(config), "discord", "check", str(scout_dir / "nope.jsonl")]) == 2


def test_a_file_with_every_line_refused_is_not_a_capture(cfg: ScoutConfig, paths: ScoutPaths,
                                                         clock: ManualClock) -> None:
    """Honest coverage: posted_at in local time on every line stores nothing, so it must not read `ok`."""
    board = SourceStatusBoard(cfg)
    inbox = _inbox(cfg, paths, clock, board)
    bad = [line("open-outcrier", NOW - timedelta(minutes=m), posted_at="2026-11-02T08:30:00Z") for m in (5, 9)]
    write_capture(paths, "open-outcrier", NOW - timedelta(minutes=1), bad)
    assert inbox.poll(clock.now()) == 0
    state = board.sources["discord_inbox"]
    assert state.status == "stale" and state.last_error == "no_capture_yet"
    assert read_jsonl(paths.discord / "files.jsonl")[0]["capture_at"] is None  # read once, not a capture
    assert "open-outcrier" not in last_captures(paths)
    clock.advance(60)
    assert _inbox(cfg, paths, clock, SourceStatusBoard(cfg)).publish(clock.now()).status == "stale"  # after a restart
    # one accepted line (or an already-stored repeat) makes it a capture again
    write_capture(paths, "open-outcrier", NOW, bad + [line("open-outcrier", NOW - timedelta(minutes=2))])
    assert inbox.poll(clock.now()) == 1 and board.sources["discord_inbox"].status == "ok"


def test_a_poison_line_neither_stops_the_poll_nor_repeats(cfg: ScoutConfig, paths: ScoutPaths,
                                                          clock: ManualClock) -> None:
    good = line("open-outcrier", NOW - timedelta(minutes=8), "$ACME invented")
    poison = json.dumps(good).replace('"Invented Author"', '"x \\udcff y"')
    write_capture(paths, "open-outcrier", NOW - timedelta(minutes=5), [poison])
    write_capture(paths, "open-outcrier", NOW - timedelta(minutes=1), [good])
    write_capture(paths, "stockmktnewz", NOW, [line("stockmktnewz", NOW - timedelta(minutes=3), "$BETA invented")])
    board = SourceStatusBoard(cfg)
    inbox = _inbox(cfg, paths, clock, board)
    assert inbox.poll(clock.now()) == 2
    assert {f["ticker"] for f in _facts(paths, "discord")} == {"ACME", "BETA"}
    reasons = [r["reason"] for r in read_jsonl(paths.discord / "rejects" / "2026-11-02.jsonl")]
    assert reasons == ["text_not_utf8"] and board.sources["discord_inbox"].status == "ok"
    assert inbox.poll(clock.now()) == 0 and len(read_jsonl(paths.discord / "files.jsonl")) == 3


def test_an_unexpected_error_books_the_file_and_the_poll_goes_on(cfg: ScoutConfig, paths: ScoutPaths,
                                                                 clock: ManualClock, monkeypatch) -> None:
    write_capture(paths, "brianna-news", NOW - timedelta(minutes=4), [line("brianna-news", NOW - timedelta(minutes=6))])
    write_capture(paths, "open-outcrier", NOW, [line("open-outcrier", NOW - timedelta(minutes=2), "$ACME invented")])
    inbox = _inbox(cfg, paths, clock)
    real = inbox._post_facts

    def boom(post, *a):
        if post.channel == "brianna-news":
            raise RuntimeError("invented failure")
        return real(post, *a)

    monkeypatch.setattr(inbox, "_post_facts", boom)
    assert inbox.poll(clock.now()) == 1
    booked = {r["file"]: r for r in read_jsonl(paths.discord / "files.jsonl")}
    assert len(booked) == 2
    bad = next(r for k, r in booked.items() if k.startswith("brianna-news/"))
    assert bad["capture_at"] is None and bad["rejects"] == 1
    reasons = [r["reason"] for r in read_jsonl(paths.discord / "rejects" / "2026-11-02.jsonl")]
    assert reasons == ["ingest_error:RuntimeError"]
    assert inbox.poll(clock.now()) == 0  # booked: not retried every poll


def test_a_disk_error_leaves_the_file_for_the_next_poll(cfg: ScoutConfig, paths: ScoutPaths, clock: ManualClock,
                                                        monkeypatch) -> None:
    write_capture(paths, "open-outcrier", NOW, [line("open-outcrier", NOW - timedelta(minutes=2), "$ACME invented")])
    inbox = _inbox(cfg, paths, clock)
    real = inbox.raw.save
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("invented")
        return real(*a, **k)

    monkeypatch.setattr(inbox.raw, "save", flaky)
    assert inbox.poll(clock.now()) == 0 and read_jsonl(paths.discord / "files.jsonl") == []
    assert inbox.poll(clock.now()) == 1
