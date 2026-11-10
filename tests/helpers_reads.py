"""Factories for the thesis-read tests: Radar evidence packs shaped as READS_TEST.md 4.1, the
model's answers (5.3) and a Radar folder beside the test's Scout folder. No network, no real key."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from helpers_fake_anthropic import message, usage

__all__ = ["DAY", "answer", "append_pack", "pack", "radar_dir", "read_reply"]

DAY = "2026-11-02"


def radar_dir(scout_dir: Path) -> Path:
    d = scout_dir.parent / "Radar"
    (d / "state" / "handoff" / "packs").mkdir(parents=True, exist_ok=True)
    return d


def _z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def pack(symbol: str = "ABCD", candidate_id: int = 7, seq: int = 0, made_at: datetime | None = None, *,
         session: str = "CORE", day: str = DAY, bid: str | None = "2.50", ask: str | None = "2.52",
         **over: Any) -> dict[str, Any]:
    """A pack as Radar writes it; any key may be overridden (and any may be null)."""
    made = made_at or datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
    t0 = int((made - timedelta(minutes=5)).timestamp()) // 60 * 60
    bars = [[t0 + 60 * i, 2.40 + 0.02 * i, 2.45 + 0.02 * i, 2.38 + 0.02 * i, 2.44 + 0.02 * i, 12000 + 500 * i,
             "w" if i < 4 else "p"] for i in range(5)]
    rec: dict[str, Any] = {
        "format": 1, "kind": "pack", "pack_id": f"{day}:{candidate_id}:{seq}", "candidate_id": candidate_id,
        "seq": seq, "reason": "find" if seq == 0 else "refresh", "symbol": symbol, "trading_day": day,
        "session": session, "made_at": _z(made),
        "quote": None if bid is None and ask is None else {
            "at": _z(made - timedelta(seconds=2)), "bid": bid, "ask": ask, "bid_size": "1200", "ask_size": "800",
            "last": "2.51", "last_at": _z(made - timedelta(seconds=3)), "spread_pct": "0.0079"},
        "day": {"prev_close": "2.00", "open": "2.10", "high": "2.60", "low": "2.05", "volume": "4500000",
                "change_pct": "0.255", "gap_pct": "0.05", "premarket_high": "2.30", "premarket_low": "2.01",
                "vwap": "2.31"},
        "bars_1m": bars,
        "bars_1d": [["2026-10-29", 1.9, 2.05, 1.85, 2.0, 900000], ["2026-10-30", 2.0, 2.1, 1.95, 2.0, 800000]]
        if seq == 0 else None,
        "atr5": "0.045", "rel_volume_10d": "5.2",
        "find": {"found_at": _z(made - timedelta(seconds=40)), "feeds": ["gainers"], "price": "2.48",
                 "change_pct": "0.24", "tags": ["news"], "flags": []} if seq == 0 else None,
        "checks": {"at": _z(made - timedelta(seconds=10)), "ready": False, "failed": ["spread"]},
        "market": {"at": _z(made - timedelta(seconds=30)), "SPY": {"last": "580.10", "change_pct": "0.004"},
                   "QQQ": {"last": "500.20", "change_pct": "0.006"}, "IWM": {"last": "220.00", "change_pct": "-0.001"}},
        "radar_release": "test",
    }
    rec.update(over)
    return rec


def append_pack(radar: Path, rec: dict[str, Any] | str | bytes, day: str = DAY) -> Path:
    """Append one line (a pack, or raw text/bytes for a broken line) to Radar's packs file."""
    path = radar / "state" / "handoff" / "packs" / f"{day}.packs.jsonl"
    if isinstance(rec, dict):
        data = (json.dumps(rec, separators=(",", ":")) + "\n").encode("utf-8")
    else:
        data = rec if isinstance(rec, bytes) else rec.encode("utf-8")
    with open(path, "ab") as fh:
        fh.write(data)
    return path


def answer(direction: str = "long", strength: int = 4, status: str = "new", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "direction": direction, "strength": strength, "horizon_min": 30,
        "invalidation": 2.40 if direction == "long" else (2.70 if direction == "short" else None),
        "target": 2.80 if direction == "long" else (2.20 if direction == "short" else None),
        "reasons": [{"kind": "volume", "side": "for", "note": "Volume five times normal."},
                    {"kind": "book", "side": "against", "note": "Spread near 0.8%."}],
        "fade_if": "Loses VWAP on volume.", "status": status,
    }
    base.update(over)
    return base


def read_reply(obj: Any = None, *, stop_reason: str = "end_turn", model: str = "claude-haiku-5-5",
               input_tokens: int = 3000, output_tokens: int = 600) -> Any:
    return message(answer() if obj is None else obj, stop_reason=stop_reason, model=model,
                   use=usage(input_tokens=input_tokens, output_tokens=output_tokens))
