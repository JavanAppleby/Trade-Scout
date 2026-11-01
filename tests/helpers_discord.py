"""Helpers for the Discord inbox tests: synthetic capture lines (invented text only), snowflake ids
whose embedded time matches `posted_at`, and capture files written the way a task writes them."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from scout.clock import iso
from scout.discord_parse import DISCORD_EPOCH_MS
from scout.paths import ScoutPaths

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "scout_inbox"
SERVER = "111111111111111111"
CHANNEL_ID = "222222222222222299"


def snowflake(at: datetime, worker: int = 7) -> str:
    """A Discord message id created at `at` (ms precision)."""
    ms = int(at.timestamp() * 1000)
    return str(((ms - DISCORD_EPOCH_MS) << 22) | worker)


def line(channel: str, posted: datetime, text: str = "Invented test post", *, kind: str = "post",
         worker: int = 7, captured: datetime | None = None, **over: Any) -> dict[str, Any]:
    mid = snowflake(posted, worker)
    rec: dict[str, Any] = {"format": 1, "channel": channel, "message_id": mid,
                           "permalink": f"https://discord.com/channels/{SERVER}/{CHANNEL_ID}/{mid}",
                           "posted_at": iso(posted.replace(microsecond=0)),
                           "captured_at": iso((captured or posted + timedelta(minutes=2)).replace(microsecond=0)),
                           "author": "Invented Author", "text": text, "image_text": None, "kind": kind}
    rec.update(over)
    return rec


def schedule(channel: str, posted: datetime, day: str, session: str, tickers: list[str], **over: Any) -> dict[str, Any]:
    return line(channel, posted, f"Top Earnings {day} {session}: " + " ".join("$" + t for t in tickers),
                kind="earnings_schedule", earnings=[{"date": day, "session": session, "tickers": tickers}], **over)


def capture_name(at: datetime) -> str:
    return at.strftime("%Y%m%dT%H%M%SZ") + ".jsonl"


def write_capture(paths: ScoutPaths, channel: str, at: datetime, rows: list[Any], *, tail: str = "",
                  name: str | None = None) -> Path:
    """A capture file as a task writes it (temp file, then rename). `rows` may hold raw strings."""
    folder = paths.inbox_discord / channel
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (name or capture_name(at))
    body = "".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows) + tail
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(body, encoding="utf-8", newline="\n")
    tmp.replace(target)
    return target


def copy_fixture_inbox(paths: ScoutPaths) -> None:
    """The synthetic fixture inbox (tests/fixtures/scout_inbox) copied into a Scout folder's inbox."""
    shutil.copytree(FIXTURES / "discord", paths.inbox_discord, dirs_exist_ok=True)
    shutil.copytree(FIXTURES / "jason", paths.inbox_jason, dirs_exist_ok=True)


def copy_scanner_fixtures(paths: ScoutPaths) -> None:
    """The synthetic Paper Gains scanner cards (tests/fixtures/scout_inbox/scanners/<channel>/) copied into
    a Scout folder's Discord inbox. Kept apart from `discord/` so the other fixture counts stay as they are."""
    shutil.copytree(FIXTURES / "scanners", paths.inbox_discord, dirs_exist_ok=True)


def scanner_card(channel: str, posted: datetime, timeframe: str, pattern: str, state: str | None,
                 tickers: list[Any], *, worker: int = 9, **over: Any) -> dict[str, Any]:
    """A `scanner_alert` capture line: the card text verbatim plus its `scanner` object."""
    title = f"{pattern} - {timeframe if timeframe == '15m' else timeframe.title()}" + (f" ({state})" if state else "")
    text = title + "\nTickers\n" + "\n".join(str(t) for t in tickers) + "\nDisclaimer"
    rec = line(channel, posted, text, kind="scanner_alert", worker=worker,
               scanner={"timeframe": timeframe, "pattern": pattern, "state": state, "tickers": tickers})
    rec["author"] = "PaperGains Scanner"
    rec.update(over)
    return rec
