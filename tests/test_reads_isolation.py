"""Reads change no answer (owner, 2026-11-10; READS_TEST.md 2; SCOUT_SPEC 2.7): nothing in the
answers, the gates, `entry_blocks` or `scout_reader` reads the reads files."""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scout_reader.api import Reader

from conftest import make_config
from helpers_fake_anthropic import fact
from helpers_fakes import write_records
from helpers_reads import DAY
from scout.clock import ManualClock
from scout.paths import ScoutPaths
from scout.reads_file import ReadsWriter

ROOT = Path(__file__).resolve().parent.parent
AS_OF = datetime(2026, 11, 2, 23, 0, tzinfo=timezone.utc)
# the only Scout modules that may name the reads files (writers, the service and the brief's count)
ALLOWED = {"paths.py", "reads_file.py", "reads_owner.py", "reads_service.py", "reads_tail.py", "reads_report.py",
           "jobs_reads.py", "reads_budget.py", "reads_context.py", "reads_prompt.py", "config_reads.py",
           "reads_market.py", "market_feed.py"}  # 0.3.2: the market feed for the reads
ANSWERING = ("answers.py", "answers_cli.py", "jobs_answers.py", "verify.py", "verify_check.py", "due_window.py")

_opened: list[str] = []
_watching: list[bool] = [False]


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if _watching[0] and event == "open" and args and isinstance(args[0], (str, Path)):
        _opened.append(str(args[0]))


sys.addaudithook(_hook)


def test_no_reader_or_answer_module_names_the_reads_files() -> None:
    pattern = re.compile(r"state[/\\\\]+reads|\bpaths\.reads\b|\bREADS\b|inbox_reads|reads_on\b|reads_file\b")
    for path in (ROOT / "scout_reader" / "scout_reader").glob("*.py"):
        assert not pattern.search(path.read_text(encoding="utf-8")), path.name
    for path in (ROOT / "scout").glob("*.py"):
        if path.name in ALLOWED:
            continue
        assert not pattern.search(path.read_text(encoding="utf-8")), f"{path.name} names the reads files"
    for name in ANSWERING:
        text = (ROOT / "scout" / name).read_text(encoding="utf-8")
        assert "reads_" not in text and "scout.reads" not in text, name


def _answers(scout_dir: Path) -> dict[str, Any]:
    r = Reader(scout_dir, cache=False)
    out: dict[str, Any] = {}
    for sym in ("ABCD", "WXYZ"):
        out[sym] = {
            "entry_blocks": r.entry_blocks(sym, AS_OF), "news": r.news_headline(sym, AS_OF),
            "fresh": r.fresh_catalyst(sym, AS_OF), "event": r.event_before_next_session(sym, AS_OF),
            "halt": r.halt_open(sym, AS_OF), "jason": r.jason_tags(sym, 2.5, AS_OF),
        }
    out["market"] = r.market_context(AS_OF)
    return out


def test_reads_change_no_answer(scout_dir: Path) -> None:
    cfg = make_config(scout_dir, llm_enabled=True, reads_enabled=True)
    paths = ScoutPaths.from_config(cfg)
    write_records(scout_dir, "facts", [fact("ABCD", "ABCD wins contract", "2026-11-02T14:30:00Z", fid="f1")])
    before = _answers(scout_dir)
    writer = ReadsWriter(paths, ManualClock(AS_OF))
    for sym in ("ABCD", "WXYZ"):
        writer.append({"format": 1, "kind": "first", "reader": "R1", "symbol": sym, "trading_day": DAY,
                       "direction": "long", "strength": 5, "status": "new", "pack_id": f"{DAY}:1:0"}, number=True)
        writer.append({"format": 1, "kind": "owner", "reader": "owner", "symbol": sym, "trading_day": DAY,
                       "direction": "short", "strength": 1, "status": "new"}, number=True)
    _opened.clear()
    _watching[0] = True
    try:
        after = _answers(scout_dir)
    finally:
        _watching[0] = False
    assert _opened  # the hook saw the reader's own reads of Scout's files
    assert after == before
    reads_dir = str(paths.reads).lower()
    assert not [p for p in _opened if p.lower().startswith(reads_dir)]
