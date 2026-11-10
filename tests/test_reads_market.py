"""What a reader is shown of the market (0.3.2; SCOUT_SPEC 19.5, DESIGN 24): the newest fresh block
of Scout's feed or the pack, never one first seen after the read, and the read line says which."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from helpers_reads import DAY, pack, read_reply
from test_reads_service import MADE, Rig

from scout.paths import ScoutPaths
from scout.reads_file import iso_us
from scout.reads_market import (
    from_pack, from_record, latest_record, market_file, market_line, shown_market,
)
from scout.store import append_jsonl

NOW = MADE + timedelta(seconds=5)  # 10:00:05 ET on 2026-11-02 (EST)


def record(at: datetime, seen: datetime, *, feed: str = "iex", spy: str = "681.2") -> dict[str, Any]:
    syms = {"SPY": {"last": spy, "change_pct": "0.003094", "prev_close": "679.10", "trade_at": iso_us(at)},
            "QQQ": {"last": "611.05", "change_pct": "0.004201", "prev_close": "608.49", "trade_at": iso_us(at)},
            "IWM": {"last": "241.13", "change_pct": "-0.002152", "prev_close": "241.65", "trade_at": iso_us(at)}}
    return {"format": 1, "kind": "market", "first_seen_at": iso_us(seen), "at": iso_us(at), "feed": feed,
            "prev_close_feed": "sip", "prev_day": "2026-10-30", "trading_day": DAY, "symbols": syms}


def write(paths: ScoutPaths, *recs: dict[str, Any]) -> None:
    for rec in recs:
        append_jsonl(paths, market_file(paths, DAY), rec)


def test_no_look_ahead(scout_dir: Path) -> None:
    paths = ScoutPaths(scout_dir)
    write(paths, record(NOW - timedelta(seconds=60), NOW - timedelta(seconds=58), spy="1.00"),
          record(NOW - timedelta(seconds=2), NOW + timedelta(seconds=1), spy="2.00"))  # first seen after now
    rec = latest_record(paths, NOW)
    assert rec is not None and rec["symbols"]["SPY"]["last"] == "1.00"
    assert latest_record(paths, NOW + timedelta(seconds=1))["symbols"]["SPY"]["last"] == "2.00"
    assert latest_record(paths, NOW - timedelta(seconds=59)) is None


def test_max_age_boundary() -> None:
    rec = record(NOW - timedelta(seconds=1800), NOW - timedelta(seconds=1790))
    assert shown_market(None, rec, NOW, 1800) is not None
    assert shown_market(None, rec, NOW + timedelta(microseconds=1), 1800) is None
    future = record(NOW + timedelta(seconds=61), NOW)
    assert shown_market(None, future, NOW, 1800) is None  # a time over 60 s ahead of Scout's clock is not shown
    near = record(NOW + timedelta(seconds=60), NOW)
    assert shown_market(None, near, NOW, 1800) is not None  # up to 60 s ahead: a PC clock a little slow
    old_pack = {"at": iso_us(NOW - timedelta(seconds=1801)), "SPY": {"last": "1", "change_pct": "0"}}
    assert shown_market(old_pack, None, NOW, 1800) is None  # the same rule for the pack's own block


def test_pack_or_scout_whichever_newer() -> None:
    mine = record(NOW - timedelta(seconds=30), NOW - timedelta(seconds=28))
    newer_pack = {"at": iso_us(NOW - timedelta(seconds=10)), "SPY": {"last": "580.10", "change_pct": "0.004"}}
    older_pack = {"at": iso_us(NOW - timedelta(seconds=40)), "SPY": {"last": "580.10", "change_pct": "0.004"}}
    assert shown_market(newer_pack, mine, NOW, 1800)["source"] == "radar"
    assert shown_market(older_pack, mine, NOW, 1800)["source"] == "scout"
    same = {"at": mine["at"], "SPY": {"last": "580.10", "change_pct": "0.004"}}
    assert shown_market(same, mine, NOW, 1800)["source"] == "scout"  # a tie goes to Scout's record
    assert shown_market(None, None, NOW, 1800) is None
    block = shown_market(None, mine, NOW, 1800, ("SPY", "QQQ", "IWM"))
    assert block == {"source": "scout", "feed": "iex", "at": mine["at"],
                     "symbols": {"SPY": {"last": "681.2", "change_pct": "0.003094"},
                                 "QQQ": {"last": "611.05", "change_pct": "0.004201"},
                                 "IWM": {"last": "241.13", "change_pct": "-0.002152"}}}
    assert list(block["symbols"]) == ["SPY", "QQQ", "IWM"]  # the configured order, not the file's


def test_prompt_lines_exact() -> None:
    fresh = from_record(record(NOW - timedelta(seconds=3), NOW), ("SPY", "QQQ", "IWM"))
    today = NOW.astimezone(timezone(timedelta(hours=-5))).date()
    assert market_line(fresh, NOW, today) == "Market at 10:00:02 ET: SPY 681.20 +0.31%; QQQ 611.05 +0.42%; IWM 241.13 -0.22%"
    old = from_record(record(NOW - timedelta(minutes=15, seconds=50), NOW), ("SPY", "QQQ", "IWM"))
    assert market_line(old, NOW, today) == ("Market at 09:44:15 ET (15 minutes before now): SPY 681.20 +0.31%; "
                                            "QQQ 611.05 +0.42%; IWM 241.13 -0.22%")
    edge = from_record(record(NOW - timedelta(seconds=300), NOW), ("SPY",))
    assert "(5 minutes before now)" in market_line(edge, NOW, today)
    assert "before now" not in market_line(from_record(record(NOW - timedelta(seconds=299), NOW)), NOW, today)
    assert market_line(None, NOW, today) == "Market: not available"
    assert market_line(from_pack({"at": iso_us(NOW), "SPY": {"last": "580.1", "change_pct": "0.004"}}), NOW,
                       today) == "Market at 10:00:05 ET: SPY 580.10 +0.40%"


def test_read_line_stores_market_shown_or_null(scout_dir: Path) -> None:
    rig = Rig(scout_dir, read_reply(), read_reply())
    write(rig.paths, record(MADE - timedelta(seconds=20), MADE - timedelta(seconds=18)))
    (line,) = rig.feed(pack(seq=0, made_at=MADE, market=None))
    shown = line["market_shown"]
    assert shown["source"] == "scout" and shown["feed"] == "iex" and shown["symbols"]["SPY"]["last"] == "681.2"
    assert list(shown["symbols"]) == ["IWM", "QQQ", "SPY"]  # stored with sorted keys
    user = rig.tr.calls[0]["messages"][0]["content"]
    text = user if isinstance(user, str) else json.dumps(user)
    assert "Market at 09:59:40 ET: SPY 681.20 +0.31%; QQQ 611.05 +0.42%; IWM 241.13 -0.22%" in text
    # with neither block fresh: null, and the prompt says so
    rig.at(60)
    lines = rig.feed(pack(candidate_id=8, seq=0, made_at=MADE + timedelta(minutes=60), market=None))
    assert lines[-1]["market_shown"] is None
    user = rig.tr.calls[1]["messages"][0]["content"]
    assert "Market: not available" in (user if isinstance(user, str) else json.dumps(user))


def test_switched_off_feed_is_not_shown(scout_dir: Path) -> None:
    rig = Rig(scout_dir, read_reply(), reads_market_enabled=False)
    write(rig.paths, record(MADE - timedelta(seconds=20), MADE - timedelta(seconds=18)))
    (line,) = rig.feed(pack(seq=0, made_at=MADE))  # the pack's own block (30 s old) is still shown
    assert line["market_shown"]["source"] == "radar"


def test_bad_market_lines_skipped(scout_dir: Path) -> None:
    paths = ScoutPaths(scout_dir)
    good = record(NOW - timedelta(seconds=60), NOW - timedelta(seconds=58))
    write(paths, good)
    path = market_file(paths, DAY)
    with open(path, "ab") as fh:
        fh.write(b'{"format":1,"kind":"market","first_seen_at":"' + iso_us(NOW).encode() + b'","at":"bad"}\n')
        fh.write(b"not json\n")
        fh.write(b'{"format":1,"kind":"miss","first_seen_at":"' + iso_us(NOW).encode() + b'","reason":"stale"}\n')
        fh.write(b'{"format":1,"kind":"market","first_seen_at":"' + iso_us(NOW).encode() + b'","at":"'
                 + iso_us(NOW).encode() + b'","symbols":{"SPY":{"last":"-1","change_pct":"0"}}}\n')
        fh.write(b'{"partial')
    assert latest_record(paths, NOW) == good
    assert from_record({"kind": "market", "at": iso_us(NOW), "symbols": []}) is None
    assert from_pack({"at": "x", "SPY": {"last": "1", "change_pct": "0"}}) is None and from_pack(None) is None
    # a pack row without a number is left out, the rest is kept
    assert list(from_pack({"at": iso_us(NOW), "SPY": {"last": "1", "change_pct": None},
                           "QQQ": {"last": "2", "change_pct": "0.1"}})["symbols"]) == ["QQQ"]

