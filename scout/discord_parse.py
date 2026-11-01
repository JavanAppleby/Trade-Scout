"""Paper Gains Discord capture files: the strict per-line check (docs/INBOX.md, DESIGN 2.7). Pure: no IO.

A scheduled Claude task (Claude in Chrome, the owner's logged-in Discord) writes one capture file per
channel per run, `inbox\\discord\\<channel>\\<YYYYMMDDTHHMMSSZ>.jsonl`, one JSON object per post. Scout never
reads Discord itself. Everything in a capture is untrusted third-party text transcribed by an LLM task
(an image transcription even more so): it is checked here field by field, and a line that fails any
check is refused with a reason while the rest of the file is kept.

Checks (each failure is one reason string; the first failing check names the line):
- the line is one JSON object with exactly the INBOX format-1 keys (`earnings` optional), `format` 1;
- `channel` is the folder's channel and one of `discord_channels`;
- `message_id` is a Discord snowflake (digits) and `permalink` is
  `https://discord.com/channels/<server>/<channel>/<message_id>`;
- `posted_at` is UTC ISO with `Z`, agrees with the time inside the message id within
  `discord_posted_at_tolerance_s`, is not in the future and not older than `discord_max_post_age_days`;
- `captured_at` is UTC ISO with `Z`, not before `posted_at` (less the tolerance) and not in the future;
- `author` and `text` are strings, `image_text` a string or null (both cut to their config caps), and
  each encodes as UTF-8 (a lone surrogate escape such as "\\udcff" is refused: `text_not_utf8`);
- `kind` is one of `KINDS`; `scanner` is required for `scanner_alert` and refused elsewhere, and a
  `scanner_alert` is allowed only in the three scanner channels (`SCANNER_TIMEFRAMES`: folder
  15m-scanner / daily-scanner / weekly-scanner, timeframe 15m / daily / weekly, which must agree):
  `{timeframe, pattern (as printed, at most MAX_PATTERN_CHARS), state (as printed in parentheses, or
  null), tickers: [plain tickers, at least one]}`;
- `earnings` is required for `earnings_schedule`, allowed for `levels_card`,
  refused elsewhere, and allowed at all only in `discord_schedule_channels`. Each entry is
  `{date: "YYYY-MM-DD" (ET), session: "pre"|"post"|"unknown", tickers: [...]}` with the date no earlier
  than the post's ET date less one day and no later than `discord_schedule_max_ahead_days` after it.
  A `levels_card` with neither an AM ER nor a PM ER line may carry `earnings: []` (read as no list);
  an `earnings_schedule` line needs at least one entry.

Reasons never carry capture text: an unknown key is named only when it is a plain key of at most 20
characters (at most three, then "+N"), and a refused line keeps its `message_id` only when it is a
snowflake.

Tickers named by a post (`post_tickers`): `$TICKER` cashtags, exchange tags "(NASDAQ: XXX)" (the wire
rule), and in `discord_prefix_ticker_channels` a leading "TICKER - headline". All go through
`scout.tickers.normalise`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from scout.clock import et_date
from scout.tickers import normalise, tickers_in

__all__ = [
    "CAPTURE_NAME", "FORMAT", "KINDS", "MAX_ENTRIES", "MAX_SCHEDULE_TICKERS", "OPTIONAL_KEYS", "REQUIRED_KEYS",
    "SCANNER_TIMEFRAMES", "SESSIONS", "Capture", "EarningsEntry", "Post", "Reject", "Rules", "ScannerAlert",
    "capture_time", "describe", "parse_capture", "post_tickers", "rules_from", "snowflake_time",
    "split_capture_name", "validate_line",
]

FORMAT = 1
KINDS = ("post", "earnings_schedule", "levels_card", "recap", "scanner_alert")
SESSIONS = ("pre", "post", "unknown")
REQUIRED_KEYS = frozenset({"format", "channel", "message_id", "permalink", "posted_at", "captured_at", "author",
                           "text", "image_text", "kind"})
OPTIONAL_KEYS = frozenset({"earnings", "scanner"})
#: Format limits (docs/INBOX.md), not tunables: a schedule post lists at most this many dated entries,
#: each at most this many tickers; an author name at most this many characters.
MAX_ENTRIES = 40
MAX_SCHEDULE_TICKERS = 400
MAX_AUTHOR_CHARS = 100
#: The Paper Gains SCANNERS channels (owner 2026-11-01): folder name -> the only timeframe its cards carry.
#: A format fact, not a tunable: a `scanner_alert` line is refused in any other folder.
SCANNER_TIMEFRAMES = {"15m-scanner": "15m", "daily-scanner": "daily", "weekly-scanner": "weekly"}
SCANNER_KEYS = frozenset({"timeframe", "pattern", "state", "tickers"})
#: Format limits for a scanner card: pattern and state as printed, and the ticker list.
MAX_PATTERN_CHARS = 80
MAX_STATE_CHARS = 40
MAX_SCANNER_TICKERS = 200
#: Discord's snowflake epoch (2015-01-01T00:00:00Z) in milliseconds: a message id holds its own time.
DISCORD_EPOCH_MS = 1_420_070_400_000

CAPTURE_NAME = re.compile(r"^(\d{8}T\d{6}Z)\.jsonl$")
_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_SNOWFLAKE = re.compile(r"^\d{15,22}$")
#: An unknown key is named in a reason only when it looks like a plain key; reasons are written to the
#: rejects file, the health record, the log and the brief, so capture text never reaches them raw.
_PLAIN_KEY = re.compile(r"^[A-Za-z0-9_]{1,20}$")
MAX_NAMED_KEYS = 3
_PERMALINK = re.compile(r"^https://discord\.com/channels/(\d{5,22})/(\d{5,22})/(\d{15,22})$")
_TICKER = re.compile(r"^[A-Z][A-Z0-9]{0,5}(?:[.\-][A-Z0-9]{1,2})?$")
_CASHTAG = re.compile(r"(?<![A-Za-z0-9$])\$([A-Za-z][A-Za-z0-9]{0,5}(?:[.\-][A-Za-z]{1,2})?)(?![A-Za-z0-9])")
_PREFIX = re.compile(r"^\s*([A-Z][A-Z0-9]{0,5}(?:\.[A-Z]{1,2})?)\s+[-–—:]\s+\S")


@dataclass(frozen=True)
class Rules:
    """The config values the check needs (`rules_from(cfg)`)."""

    channels: frozenset[str]
    schedule_channels: frozenset[str]
    prefix_channels: frozenset[str]
    text_max: int = 2000
    image_max: int = 4000
    max_age_days: int = 14
    tolerance_s: int = 120
    max_ahead_days: int = 14
    exchange_prefixes: tuple[str, ...] = ()


def rules_from(cfg: Any) -> Rules:
    return Rules(channels=frozenset(cfg.discord_channels), schedule_channels=frozenset(cfg.discord_schedule_channels),
                 prefix_channels=frozenset(cfg.discord_prefix_ticker_channels), text_max=cfg.discord_text_max_chars,
                 image_max=cfg.discord_image_text_max_chars, max_age_days=cfg.discord_max_post_age_days,
                 tolerance_s=cfg.discord_posted_at_tolerance_s, max_ahead_days=cfg.discord_schedule_max_ahead_days,
                 exchange_prefixes=tuple(cfg.ticker_exchange_prefixes))


@dataclass(frozen=True)
class EarningsEntry:
    date: str  # ET calendar date YYYY-MM-DD
    session: str  # pre | post | unknown
    tickers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"date": self.date, "session": self.session, "tickers": list(self.tickers)}


@dataclass(frozen=True)
class ScannerAlert:
    timeframe: str  # 15m | daily | weekly (agrees with the channel folder)
    pattern: str  # as printed, e.g. "Outside Bar"
    state: str | None  # as printed in parentheses, e.g. "In Force"; None when the title has none
    tickers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"timeframe": self.timeframe, "pattern": self.pattern, "state": self.state,
                "tickers": list(self.tickers)}


@dataclass(frozen=True)
class Post:
    channel: str
    message_id: str
    permalink: str
    posted_at: str
    captured_at: str
    author: str
    text: str
    text_truncated: bool
    image_text: str | None
    image_truncated: bool
    kind: str
    earnings: tuple[EarningsEntry, ...]
    tickers: tuple[str, ...]
    line_no: int
    scanner: ScannerAlert | None = None


@dataclass(frozen=True)
class Reject:
    line_no: int
    reason: str
    message_id: str | None = None


@dataclass
class Capture:
    channel: str
    name: str
    posts: list[Post] = field(default_factory=list)
    rejects: list[Reject] = field(default_factory=list)
    lines: int = 0


# --------------------------------------------------------------------------- names and times


def split_capture_name(name: str) -> str | None:
    """The `YYYYMMDDTHHMMSSZ` stem of a capture file name (`.jsonl`, or `.jsonl.tmp` before the rename)."""
    base = name[:-4] if name.endswith(".tmp") else name
    m = CAPTURE_NAME.match(base)
    return m.group(1) if m else None


def capture_time(name: str) -> datetime | None:
    """The UTC time a capture file's name carries, or None."""
    stem = split_capture_name(name)
    if stem is None:
        return None
    try:
        return datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def snowflake_time(message_id: str) -> datetime | None:
    """The creation time inside a Discord message id (a snowflake), or None."""
    if not isinstance(message_id, str) or not _SNOWFLAKE.match(message_id):
        return None
    ms = (int(message_id) >> 22) + DISCORD_EPOCH_MS
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not _ISO_Z.match(value):
        return None
    try:
        return datetime.fromisoformat(value[:-1]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------- tickers


def post_tickers(text: str, channel: str, rules: Rules) -> list[str]:
    """Every ticker the post's text names, in order, without repeats."""
    out: list[str] = []

    def add(sym: str) -> None:
        t = normalise(sym)
        if _TICKER.match(t) and t not in out:
            out.append(t)

    if channel in rules.prefix_channels:
        first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
        m = _PREFIX.match(first)
        if m:
            add(m.group(1))
    for m in _CASHTAG.finditer(text or ""):
        add(m.group(1).upper())
    for _exchange, sym in tickers_in(text or "", rules.exchange_prefixes):
        add(sym)
    return out


# --------------------------------------------------------------------------- one line


def _cut(text: str, limit: int) -> tuple[str, bool]:
    return (text[:limit], True) if len(text) > limit else (text, False)


def _entries(raw: object, posted: datetime, rules: Rules) -> tuple[tuple[EarningsEntry, ...], str | None]:
    if not isinstance(raw, list) or not raw:
        return (), "earnings:not_a_non_empty_list"
    if len(raw) > MAX_ENTRIES:
        return (), f"earnings:more_than_{MAX_ENTRIES}_entries"
    posted_day = et_date(posted)
    first, last = posted_day - timedelta(days=1), posted_day + timedelta(days=rules.max_ahead_days)
    out: list[EarningsEntry] = []
    for n, item in enumerate(raw):
        where = f"earnings[{n}]"
        if not isinstance(item, Mapping) or set(item) != {"date", "session", "tickers"}:
            return (), f"{where}:keys_must_be_date_session_tickers"
        try:
            day = date.fromisoformat(item["date"]) if isinstance(item["date"], str) and len(item["date"]) == 10 else None
        except ValueError:
            day = None
        if day is None:
            return (), f"{where}:date_not_YYYY-MM-DD"
        if not first <= day <= last:
            return (), f"{where}:date_out_of_range"
        if item["session"] not in SESSIONS:
            return (), f"{where}:session_not_pre_post_unknown"
        tickers = item["tickers"]
        if not isinstance(tickers, list) or len(tickers) > MAX_SCHEDULE_TICKERS:
            return (), f"{where}:tickers_not_a_list"
        clean: list[str] = []
        for t in tickers:
            sym = normalise(t.lstrip("$")) if isinstance(t, str) else ""
            if not _TICKER.match(sym):
                return (), f"{where}:bad_ticker"
            if sym not in clean:
                clean.append(sym)
        out.append(EarningsEntry(day.isoformat(), item["session"], tuple(clean)))
    return tuple(out), None


def _scanner(raw: object, channel: str) -> tuple[ScannerAlert | None, str | None]:
    """The `scanner` object of a `scanner_alert` line, or the reason it is refused."""
    if not isinstance(raw, Mapping) or set(raw) != SCANNER_KEYS:
        return None, "scanner:keys_must_be_timeframe_pattern_state_tickers"
    if raw["timeframe"] not in SCANNER_TIMEFRAMES.values():
        return None, "scanner:timeframe_not_15m_daily_weekly"
    if raw["timeframe"] != SCANNER_TIMEFRAMES.get(channel):
        return None, "scanner:timeframe_not_channel"
    pattern, state = raw["pattern"], raw["state"]
    if not isinstance(pattern, str) or not pattern.strip() or len(pattern) > MAX_PATTERN_CHARS:
        return None, "scanner:pattern_not_a_short_string"
    if state is not None and (not isinstance(state, str) or not state.strip() or len(state) > MAX_STATE_CHARS):
        return None, "scanner:state_not_a_short_string_or_null"
    if not all(_encodes(v) for v in (pattern, state) if v is not None):
        return None, "text_not_utf8"
    tickers = raw["tickers"]
    if not isinstance(tickers, list) or not tickers or len(tickers) > MAX_SCANNER_TICKERS:
        return None, "scanner:tickers_not_a_non_empty_list"
    clean: list[str] = []
    for t in tickers:
        sym = normalise(t.strip().lstrip("$")) if isinstance(t, str) else ""
        if not _TICKER.match(sym):
            return None, "scanner:bad_ticker"
        if sym not in clean:
            clean.append(sym)
    return ScannerAlert(raw["timeframe"], " ".join(pattern.split()),
                        " ".join(state.split()) if state is not None else None, tuple(clean)), None


def validate_line(obj: object, *, channel: str, now: datetime, rules: Rules, line_no: int) -> Post | Reject:
    """One parsed line -> a Post, or a Reject naming the first failed check."""
    mid = obj.get("message_id") if isinstance(obj, Mapping) else None
    mid = mid if isinstance(mid, str) and _SNOWFLAKE.match(mid) else None

    def no(reason: str) -> Reject:
        return Reject(line_no, reason, mid)

    if not isinstance(obj, Mapping):
        return no("not_an_object")
    keys = set(obj)
    missing = sorted(REQUIRED_KEYS - keys)
    if missing:
        return no("missing_key:" + ",".join(missing))
    unknown = sorted(keys - REQUIRED_KEYS - OPTIONAL_KEYS)
    if unknown:
        return no(_unknown_reason(unknown))
    if obj["format"] != FORMAT or isinstance(obj["format"], bool):
        return no("unknown_format")
    if obj["channel"] != channel:
        return no("channel_not_folder")
    if channel not in rules.channels:
        return no("channel_not_configured")
    if mid is None or not _SNOWFLAKE.match(mid):
        return no("message_id_not_a_snowflake")
    link = obj["permalink"]
    m = _PERMALINK.match(link) if isinstance(link, str) else None
    if m is None or m.group(3) != mid:
        return no("permalink_mismatch")
    posted = _utc(obj["posted_at"])
    if posted is None:
        return no("posted_at_not_utc_iso_z")
    born = snowflake_time(mid)
    if born is None or abs((posted - born).total_seconds()) > rules.tolerance_s:
        return no("posted_at_disagrees_with_message_id")
    if (posted - now).total_seconds() > rules.tolerance_s:
        return no("posted_at_in_future")
    if now - posted > timedelta(days=rules.max_age_days):
        return no("too_old")
    captured = _utc(obj["captured_at"])
    if captured is None:
        return no("captured_at_not_utc_iso_z")
    if (posted - captured).total_seconds() > rules.tolerance_s or (captured - now).total_seconds() > rules.tolerance_s:
        return no("captured_at_out_of_range")
    author = obj["author"]
    if not isinstance(author, str) or not author.strip() or len(author) > MAX_AUTHOR_CHARS:
        return no("author_not_a_short_string")
    if not isinstance(obj["text"], str):
        return no("text_not_a_string")
    image = obj["image_text"]
    if image is not None and not isinstance(image, str):
        return no("image_text_not_string_or_null")
    if not all(_encodes(v) for v in (author, obj["text"], image) if v is not None):
        return no("text_not_utf8")
    kind = obj["kind"]
    if kind not in KINDS:
        return no("kind_unknown")
    raw_earnings = obj.get("earnings")
    entries: tuple[EarningsEntry, ...] = ()
    if raw_earnings == [] and kind == "levels_card":
        raw_earnings = None  # a daily card with neither ER line: the same as no key
    if raw_earnings is not None:
        if kind not in ("earnings_schedule", "levels_card"):
            return no("earnings_not_allowed_for_kind")
        if channel not in rules.schedule_channels:
            return no("earnings_not_allowed_in_channel")
        entries, trouble = _entries(raw_earnings, posted, rules)
        if trouble:
            return no(trouble)
    elif kind == "earnings_schedule":
        return no("earnings_required_for_earnings_schedule")
    alert: ScannerAlert | None = None
    if kind == "scanner_alert":
        if channel not in SCANNER_TIMEFRAMES:
            return no("scanner_alert_not_allowed_in_channel")
        if obj.get("scanner") is None:
            return no("scanner_required_for_scanner_alert")
        alert, trouble = _scanner(obj["scanner"], channel)
        if trouble:
            return no(trouble)
    elif "scanner" in obj:
        return no("scanner_not_allowed_for_kind")
    text, text_cut = _cut(obj["text"], rules.text_max)
    image_text, image_cut = _cut(image, rules.image_max) if isinstance(image, str) else (None, False)
    return Post(channel=channel, message_id=mid, permalink=link, posted_at=obj["posted_at"],
                captured_at=obj["captured_at"], author=author.strip(), text=text, text_truncated=text_cut,
                image_text=image_text, image_truncated=image_cut, kind=kind, earnings=entries,
                tickers=alert.tickers if alert else tuple(post_tickers(text, channel, rules)), line_no=line_no,
                scanner=alert)


def _encodes(text: str) -> bool:
    """False for a str json.loads built from a lone surrogate escape: it cannot be stored as UTF-8."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _unknown_reason(keys: Sequence[object]) -> str:
    """'unknown_key:extra' / 'unknown_key:a,b,c,+2' / 'unknown_key:?' -- never raw capture text."""
    names = [k if isinstance(k, str) and _PLAIN_KEY.match(k) else "?" for k in keys]
    shown = list(dict.fromkeys(names))[:MAX_NAMED_KEYS]
    rest = len(names) - len(shown)
    return "unknown_key:" + ",".join(shown + ([f"+{rest}"] if rest > 0 else []))


# --------------------------------------------------------------------------- one file


def _lines(body: bytes) -> Iterable[tuple[int, bytes, bool]]:
    """(line number from 1, bytes, ended with a newline)."""
    parts = body.split(b"\n")
    for n, raw in enumerate(parts, start=1):
        last = n == len(parts)
        if last and not raw.strip():
            continue
        yield n, raw, not last


def parse_capture(body: bytes, *, channel: str, name: str, now: datetime, rules: Rules) -> Capture:
    """Every line of one capture file checked on its own. An empty file is a valid capture with no
    posts (the task ran and found nothing new). A last line without a newline is read only if it is a
    complete JSON object; a partial one is refused ("partial_line"). A message id repeated inside the
    file keeps its first line."""
    cap = Capture(channel=channel, name=name)
    if body.startswith(b"\xef\xbb\xbf"):
        body = body[3:]
    seen: set[str] = set()
    for line_no, raw, ended in _lines(body):
        text = raw.strip()
        if not text:
            continue
        cap.lines += 1
        try:
            obj = json.loads(text.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            cap.rejects.append(Reject(line_no, "not_json" if ended else "partial_line"))
            continue
        got = validate_line(obj, channel=channel, now=now, rules=rules, line_no=line_no)
        if isinstance(got, Reject):
            cap.rejects.append(got)
        elif got.message_id in seen:
            cap.rejects.append(Reject(line_no, "repeated_in_file", got.message_id))
        else:
            seen.add(got.message_id)
            cap.posts.append(got)
    return cap


def describe(rejects: Sequence[Reject], limit: int = 5) -> str:
    """'line 3: too_old; line 7: permalink_mismatch' (at most `limit`, then '+N more')."""
    bits = [f"line {r.line_no}: {r.reason}" for r in rejects[:limit]]
    if len(rejects) > limit:
        bits.append(f"+{len(rejects) - limit} more")
    return "; ".join(bits)
