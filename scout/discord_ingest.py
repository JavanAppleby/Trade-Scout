"""The Discord inbox: capture files in, facts out (docs/INBOX.md, DESIGN 2.7). Record and brief only.

Every `inbox_poll_s` the `discord_inbox` job lists `inbox\\discord\\<channel>\\*.jsonl` and reads each
capture file it has not read in this form (bookkeeping in `state\\discord\\files.jsonl`: file, size,
mtime, sha256). Scout never edits, moves or deletes an inbox file: the bookkeeping is the record of
what was read, so the inbox stays the capture tasks' own folder. A changed file (new sha256) is read
again; posts already stored are found by (channel, message_id) and skipped.

For each file: the bytes are kept under `state\\raw\\discord\\<ET date>\\` (as every source's are), each
line is checked on its own (`scout.discord_parse`), and refused lines go to
`state\\discord\\rejects\\<ET date>.jsonl` and the file's health record with their reasons. Each new post
gives facts (`state\\facts\\`, `first_seen_at` = now, the as-of rule):
- kind `discord`: one per ticker the text names (one with ticker null when none), source
  `discord:<channel>`, class from the deterministic title classifier on the text, url = permalink,
  published_at = posted_at. Gate 6 never reads this kind.
- kind `earnings_schedule`: one per (listed date, session, ticker) of an `earnings` list, written
  before the post's own facts, so a crash between them never leaves a post marked done without its
  schedule. Gate 5 reads these only to add `true` (`scout_reader.discord_schedule`).
- kind `scanner`: a `scanner_alert` card from a Paper Gains scanner channel gives one fact per ticker
  instead of `discord` facts (class `scanner_alert`, `extra` timeframe / pattern / state). Record and
  brief only: no gate reads this kind (`scout_reader.scanner` describes it for Radar's later tags).

Files ignored: names starting with "." or ending ".tmp" (a task's file before its rename), and
capture files whose name time is older than `discord_max_post_age_days` + 1 day.

`discord_inbox` on the heartbeat says whether captures arrive (`cadence_status`): `ok` while the
capture-hours time (`discord_capture_hours_et`, trading days) since the newest capture is at most
`discord_expected_every_min`. An empty file is a capture (the task read the channel and found nothing
new); a file with lines of which none was accepted is not (its refusals are in the health record and
the rejects file). A file that raises while it is read is booked as refused (`ingest_error:<type>`)
so it neither stops the poll nor comes back every poll; a disk error leaves it for the next poll.
No gate reads it.
"""

from __future__ import annotations

import argparse
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from scout.calendar import is_trading_day
from scout.classify import wire_class
from scout.clock import et_date, et_datetime, iso, parse_iso
from scout.config import ScoutConfig
from scout.discord_parse import (
    Capture, Post, Reject, capture_time, describe, parse_capture, rules_from, split_capture_name,
)
from scout.facts import make_fact_id
from scout.models import Fact, HealthRecord, SourceState
from scout.paths import ScoutPaths
from scout.store import append_jsonl, read_jsonl, sha256_bytes

__all__ = ["POST_KINDS", "RAW_SOURCE", "SCANNER_CLASS", "SCANNER_KIND", "STATUS_SOURCE", "DiscordInbox", "cadence_status", "cli_discord_check",
           "in_window_seconds", "last_captures"]

STATUS_SOURCE = "discord_inbox"
RAW_SOURCE = "discord"
FILES_LOG = "files.jsonl"
REJECTS_DIR = "rejects"
TITLE_CHARS = 300
SCANNER_KIND = "scanner"
SCANNER_CLASS = "scanner_alert"
#: The fact kinds that carry one Discord post each (by channel and message id): read back for the dedupe.
POST_KINDS = ("discord", SCANNER_KIND)

log = logging.getLogger("scout.discord")


# --------------------------------------------------------------------------- cadence


def in_window_seconds(start: datetime, end: datetime, cfg: ScoutConfig) -> float:
    """Seconds of [start, end] that fall inside `discord_capture_hours_et` on trading days."""
    if end <= start:
        return 0.0
    first, last = et_date(start), et_date(end)
    if (last - first).days > 40:
        return math.inf
    h0, h1 = cfg.discord_capture_hours_et
    total = 0.0
    day = first
    while day <= last:
        if is_trading_day(day):
            lo, hi = max(start, et_datetime(day, h0)), min(end, et_datetime(day, h1))
            if hi > lo:
                total += (hi - lo).total_seconds()
        day += timedelta(days=1)
    return total


def cadence_status(last_capture: datetime | None, now: datetime, cfg: ScoutConfig) -> tuple[str, str | None]:
    """("ok" | "stale" | "disabled", reason) for the heartbeat's `discord_inbox`."""
    if not cfg.discord_enabled:
        return "disabled", None
    if last_capture is None:
        return "stale", "no_capture_yet"
    gap = in_window_seconds(last_capture, now, cfg)
    if gap > cfg.discord_expected_every_min * 60:
        minutes = "many" if math.isinf(gap) else str(int(gap // 60))
        return "stale", f"no_capture_for_{minutes}_min_of_capture_hours"
    return "ok", None


def last_captures(paths: ScoutPaths, as_of: datetime | None = None) -> dict[str, dict[str, Any]]:
    """The latest bookkeeping line per channel (ingested at or before `as_of` when given)."""
    out: dict[str, dict[str, Any]] = {}
    for rec in read_jsonl(paths.discord / FILES_LOG):
        at = parse_iso(rec.get("ingested_at"))
        cap = parse_iso(rec.get("capture_at"))
        if at is None or cap is None or (as_of is not None and at > as_of):
            continue
        channel = str(rec.get("channel"))
        prev = out.get(channel)
        if prev is None or str(rec.get("capture_at")) >= str(prev.get("capture_at")):
            out[channel] = rec
    return out


# --------------------------------------------------------------------------- the inbox


@dataclass
class _Seen:
    size: int
    mtime_ns: int
    sha256: str


class DiscordInbox:
    """Reads the capture files; writes facts, raw copies, rejects, health and the bookkeeping."""

    def __init__(self, cfg: ScoutConfig, paths: ScoutPaths, clock: Any, facts: Any, raw: Any, health: Any,
                 board: Any = None) -> None:
        self.cfg, self.paths, self.clock = cfg, paths, clock
        self.facts, self.raw, self.health, self.board = facts, raw, health, board
        self.rules = rules_from(cfg)
        self.files: dict[str, _Seen] = {}
        self.last_capture: datetime | None = None
        for rec in read_jsonl(paths.discord / FILES_LOG):
            name, sha = rec.get("file"), rec.get("sha256")
            size, mtime = rec.get("size"), rec.get("mtime_ns")
            if isinstance(name, str) and isinstance(sha, str) and isinstance(size, int) and isinstance(mtime, int):
                self.files[name] = _Seen(size, mtime, sha)
            cap = parse_iso(rec.get("capture_at"))
            if cap is not None and (self.last_capture is None or cap > self.last_capture):
                self.last_capture = cap
        self.seen: set[tuple[str, str]] = set()
        today = et_date(clock.now())
        for back in range(cfg.discord_max_post_age_days + 2):
            for rec in read_jsonl(paths.day_file(paths.facts, today - timedelta(days=back))):
                if rec.get("kind") in POST_KINDS:
                    self.seen.add((str(rec.get("feed")), str(rec.get("guid"))))

    # ------------------------------------------------------------------ status

    def publish(self, now: datetime) -> SourceState:
        status, reason = cadence_status(self.last_capture, now, self.cfg)
        state = SourceState(enabled=self.cfg.discord_enabled, status=status,
                            last_ok=iso(self.last_capture) if self.last_capture else None, last_error=reason,
                            gap_open=False)
        if self.board is not None:
            self.board.update(STATUS_SOURCE, state)
        return state

    # ------------------------------------------------------------------ polling

    def candidates(self, now: datetime) -> list[tuple[str, Path]]:
        """(channel, file) for every capture file to look at, oldest name first."""
        out: list[tuple[str, Path]] = []
        try:
            folders = sorted(p for p in self.paths.inbox_discord.iterdir() if p.is_dir())
        except OSError:
            return out
        oldest = now - timedelta(days=self.cfg.discord_max_post_age_days + 1)
        for folder in folders:
            try:
                names = sorted(folder.iterdir(), key=lambda p: p.name)
            except OSError:
                continue
            for path in names:
                name = path.name
                if name.startswith(".") or name.lower().endswith(".tmp") or not path.is_file():
                    continue
                at = capture_time(name)
                if at is not None and at < oldest:
                    continue
                out.append((folder.name, path))
        return out

    def poll(self, now: datetime) -> int:
        """Read every new or changed capture file; returns the number of new posts stored."""
        new = 0
        if self.cfg.discord_enabled:
            for channel, path in self.candidates(now):
                new += self._one(channel, path, now)
        self.publish(now)
        return new

    def _one(self, channel: str, path: Path, now: datetime) -> int:
        key = f"{channel}/{path.name}"
        try:
            st = path.stat()
        except OSError:
            return 0
        known = self.files.get(key)
        if known is not None and known.size == st.st_size and known.mtime_ns == st.st_mtime_ns:
            return 0
        if st.st_size > self.cfg.discord_max_file_bytes:
            self._book(key, channel, st, "", None, now, Capture(channel, path.name, rejects=[
                Reject(0, "file_too_large")]), 0, 0)
            return 0
        try:
            body = path.read_bytes()
        except OSError:
            return 0  # tried again next poll
        sha = sha256_bytes(body)
        if known is not None and known.sha256 == sha:
            self.files[key] = _Seen(st.st_size, st.st_mtime_ns, sha)
            return 0
        if split_capture_name(path.name) is None:
            cap = Capture(channel, path.name, rejects=[Reject(0, "bad_file_name")])
            self._book(key, channel, st, sha, None, now, cap, 0, 0)
            return 0
        if channel not in self.rules.channels:
            cap = Capture(channel, path.name, rejects=[Reject(0, "channel_not_configured")])
            self._book(key, channel, st, sha, None, now, cap, 0, 0)
            return 0
        new = repeats = 0
        try:
            raw_sha = self.raw.save(RAW_SOURCE, f"{channel}-{path.name}", body, now)
            cap = parse_capture(body, channel=channel, name=path.name, now=now, rules=self.rules)
            for post in cap.posts:
                if (channel, post.message_id) in self.seen:
                    repeats += 1
                    continue
                for fact in self._schedule_facts(post, now, raw_sha):
                    self.facts.add(fact)
                made = (self._scanner_facts(post, now, raw_sha, path.name) if post.scanner is not None
                        else self._post_facts(post, now, raw_sha, path.name))
                for fact in made:
                    self.facts.add(fact)
                self.seen.add((channel, post.message_id))
                new += 1
        except OSError as exc:  # a disk problem: tried again next poll; the other files still are
            log.warning("discord inbox: %s: not read (%s); tried again next poll", key, type(exc).__name__)
            return new
        except Exception as exc:  # anything else must not stop the poll or come back every poll
            log.warning("discord inbox: %s: refused (%s)", key, type(exc).__name__)
            self._book(key, channel, st, sha, None, now, Capture(channel, path.name, rejects=[
                Reject(0, f"ingest_error:{type(exc).__name__}")]), new, repeats)
            return new
        # a file whose every line was refused is not a capture: the cadence must not call it coverage
        counts = cap.lines == 0 or new + repeats > 0
        named = capture_time(path.name)
        capture_at = (min(named, now) if named is not None else now) if counts else None
        self._book(key, channel, st, sha, capture_at, now, cap, new, repeats)
        return new

    # ------------------------------------------------------------------ records

    def _post_facts(self, post: Post, now: datetime, raw_sha: str, file_name: str) -> list[Fact]:
        source = f"discord:{post.channel}"
        cls = wire_class(post.text, self.cfg.class_keywords)
        title = _first_line(post.text) or ("(image) " + _first_line(post.image_text or "") if post.image_text else "")
        extra = {"post_kind": post.kind, "author": post.author, "text": post.text,
                 "text_truncated": post.text_truncated, "image_text": post.image_text,
                 "image_truncated": post.image_truncated, "captured_at": post.captured_at,
                 "earnings": [e.to_dict() for e in post.earnings], "tickers": list(post.tickers),
                 "capture_file": file_name}
        return [Fact(fact_id=make_fact_id(source, post.message_id, t), kind="discord", ticker=t, cik=None, cls=cls,
                     source=source, feed=post.channel, guid=post.message_id, url=post.permalink,
                     title=title[:TITLE_CHARS] or None, published_at=post.posted_at, first_seen_at=iso(now),
                     raw_sha256=raw_sha, extra=extra)
                for t in (post.tickers or (None,))]

    def _scanner_facts(self, post: Post, now: datetime, raw_sha: str, file_name: str) -> list[Fact]:
        """One `scanner` fact per ticker of a scanner card (record and brief only; no gate reads it)."""
        alert = post.scanner
        assert alert is not None
        source = f"discord:{post.channel}"
        head = f"{alert.pattern} - {alert.timeframe}" + (f" ({alert.state})" if alert.state else "")
        extra = {"post_kind": post.kind, "timeframe": alert.timeframe, "pattern": alert.pattern,
                 "state": alert.state, "tickers": list(alert.tickers), "author": post.author, "text": post.text,
                 "text_truncated": post.text_truncated, "captured_at": post.captured_at, "capture_file": file_name}
        return [Fact(fact_id=make_fact_id(source, post.message_id, t), kind=SCANNER_KIND, ticker=t, cik=None,
                     cls=SCANNER_CLASS, source=source, feed=post.channel, guid=post.message_id, url=post.permalink,
                     title=f"{t}: {head}"[:TITLE_CHARS], published_at=post.posted_at, first_seen_at=iso(now),
                     raw_sha256=raw_sha, extra=extra)
                for t in alert.tickers]

    def _schedule_facts(self, post: Post, now: datetime, raw_sha: str) -> list[Fact]:
        source = f"discord:{post.channel}"
        out = []
        for entry in post.earnings:
            guid = f"{post.message_id}|{entry.date}|{entry.session}"
            for t in entry.tickers:
                out.append(Fact(fact_id=make_fact_id(source, guid, t), kind="earnings_schedule", ticker=t, cik=None,
                                cls=None, source=source, feed=post.channel, guid=guid, url=post.permalink,
                                title=f"{t} earnings {entry.date} ({entry.session})", published_at=post.posted_at,
                                first_seen_at=iso(now), raw_sha256=raw_sha, event_kind="earnings",
                                event_source=source,
                                extra={"earnings_date": entry.date, "session": entry.session,
                                       "message_id": post.message_id, "post_kind": post.kind}))
        return out

    def _book(self, key: str, channel: str, st: Any, sha: str, capture_at: datetime | None, now: datetime,
              cap: Capture, new: int, repeats: int) -> None:
        stamp = iso(now)
        reject_file = self.paths.day_file(self.paths.discord / REJECTS_DIR, et_date(now))
        for r in cap.rejects:
            append_jsonl(self.paths, reject_file, {"format": 1, "at": stamp, "file": key, "channel": channel,
                                                   "line": r.line_no, "reason": r.reason, "message_id": r.message_id})
        result = "schema" if cap.rejects else "ok"
        self.health.record(HealthRecord(
            source=STATUS_SOURCE, feed=channel, kind="discord", enabled=True, gate6=False, start=stamp, end=stamp,
            result=result, http_status=None, items=cap.lines, new_items=new, overlap=None,
            spacing_s=float(self.cfg.discord_expected_every_min * 60), gap_before=False,
            error=describe(cap.rejects) or None))
        append_jsonl(self.paths, self.paths.discord / FILES_LOG, {
            "format": 1, "file": key, "channel": channel, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "sha256": sha, "capture_at": iso(capture_at) if capture_at else None, "ingested_at": stamp,
            "lines": cap.lines, "posts_new": new, "posts_repeat": repeats, "rejects": len(cap.rejects)})
        self.files[key] = _Seen(st.st_size, st.st_mtime_ns, sha)
        if capture_at is not None and (self.last_capture is None or capture_at > self.last_capture):
            self.last_capture = capture_at
        if cap.rejects:
            log.info("discord inbox: %s: %d line(s) refused (%s)", key, len(cap.rejects), describe(cap.rejects))


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return " ".join(line.split())
    return ""


# --------------------------------------------------------------------------- the check command


def check_file(cfg: ScoutConfig, path: Path, now: datetime) -> tuple[Capture | None, str | None]:
    """A capture file checked exactly as the job would read it, without writing anything."""
    if split_capture_name(path.name) is None:
        return None, "bad_file_name (want <YYYYMMDDTHHMMSSZ>.jsonl, or .jsonl.tmp before the rename)"
    channel = path.parent.name
    if channel not in cfg.discord_channels:
        return None, f"channel_not_configured ({channel!r} is not in discord_channels)"
    try:
        body = path.read_bytes()
    except OSError as exc:
        return None, f"unreadable ({type(exc).__name__})"
    if len(body) > cfg.discord_max_file_bytes:
        return None, "file_too_large"
    return parse_capture(body, channel=channel, name=path.name, now=now, rules=rules_from(cfg)), None


def cli_discord_check(ctx: Any, args: argparse.Namespace) -> int:
    """`discord check <file>`: 0 when every line would be accepted, 2 otherwise. Writes nothing."""
    path = Path(getattr(args, "file", ""))
    cap, problem = check_file(ctx.cfg, path, ctx.clock.now())
    if cap is None:
        print(f"REFUSED: {problem}")
        return 2
    for r in cap.rejects:
        print(f"line {r.line_no}: {r.reason}")
    print(f"{'OK' if not cap.rejects else 'REFUSED'}: {len(cap.posts)} post(s) accepted, "
          f"{len(cap.rejects)} line(s) refused")
    return 0 if not cap.rejects else 2
