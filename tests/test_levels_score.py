"""The fixed levels scorer and its bar loaders (SCOUT_SPEC 5.4).

Ported from `tests/test_phase_ao_jason_levels.py` AO50-AO53 (scored through the new scorer), plus
the four SCOUT_SPEC 5.4 fixes: readings expand through the loader, each setup's own window, a null
identical across processes, and n / INDICATIVE everywhere."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import textwrap
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers_levels import FakeBars, bar, one_setup, plan_with, write_read

from scout import levels_score as ls
from scout.clock import ManualClock
from scout.config import ScoutConfig
from scout.levels_bars import DAY_MINUTES, CsvBars, RadarBars
from scout.paths import ScoutPaths

D = Decimal
AFTER = date(2026, 9, 25)  # every 2026-09-15 window (expires 09-22) has closed


def _card(folder: Path, series: dict, cfg: ScoutConfig, today: date = AFTER) -> ls.Scorecard:
    return ls.score_folder(folder, FakeBars(series), today, cfg)


# --------------------------------------------------------------------------- AO50-AO53, ported


def test_AO50_scores_levels_and_the_trigger(tmp_path: Path, cfg: ScoutConfig) -> None:
    folder = tmp_path / "levels"
    write_read(folder, plan_with([one_setup("NVDA", [
        {"price": "213", "kind": "support"}, {"price": "300", "kind": "resistance"},
        {"price": "220", "kind": "trigger"}, {"price": "240", "kind": "target"},
        {"price": "190", "kind": "invalidation"}])]))
    bars = [bar("2026-09-15", 215, 216, 214, 215), bar("2026-09-16", 215, 222, 212, 221),
            bar("2026-09-17", 221, 245, 219, 243)]
    card = _card(folder, {"NVDA": bars}, cfg)
    outcomes = {(o.kind, str(o.price)): o.outcome for o in card.levels}
    assert outcomes[("support", "213")] == "held"  # wicked to it, closed back above
    assert outcomes[("resistance", "300")] == "expired"  # never asked the question
    assert outcomes[("invalidation", "190")] == "expired"
    assert [(t.outcome, t.detail) for t in card.triggers] == [("target", "target 240")]
    report = ls.render(card)
    assert "INDICATIVE" in report and "n=1" in report
    assert report.isascii()


def test_AO51_never_scores_the_read_against_its_own_day(tmp_path: Path, cfg: ScoutConfig) -> None:
    folder = tmp_path / "levels"
    write_read(folder, plan_with([one_setup("A", [{"price": "100", "kind": "support"}])]))
    card = _card(folder, {"A": [bar("2026-09-15", 99, 99, 90, 91)]}, cfg)
    assert card.levels == [] and card.triggers == []
    assert card.symbols_missing == {"A"}


def test_AO52_a_bar_that_touched_both_resolves_as_invalidated(tmp_path: Path, cfg: ScoutConfig) -> None:
    folder = tmp_path / "levels"
    write_read(folder, plan_with([one_setup("A", [
        {"price": "100", "kind": "trigger"}, {"price": "120", "kind": "target"},
        {"price": "90", "kind": "invalidation"}])]))
    bars = [bar("2026-09-16", 99, 101, 98, 100), bar("2026-09-17", 100, 130, 85, 125)]
    card = _card(folder, {"A": bars}, cfg)
    assert [t.outcome for t in card.triggers] == ["invalidated"]


def test_AO53_the_two_empty_states_read_differently(tmp_path: Path, cfg: ScoutConfig) -> None:
    empty = _card(tmp_path / "nothing-here", {}, cfg)
    assert "holds no level file" in ls.render(empty)
    folder = tmp_path / "levels"
    write_read(folder, plan_with([one_setup("A", [{"price": "100", "kind": "support"}])]))
    stale = _card(folder, {"A": [bar("2026-09-15", 99, 101, 98, 100)]}, cfg)
    text = ls.render(stale)
    assert "Nothing to score yet" in text and "holds no level file" not in text


# --------------------------------------------------------------------------- the four outcomes


def _flat(day: date, price: str) -> object:
    return bar(day, price, price, price, price)


def test_score_level_outcomes() -> None:
    tol = D("1")
    days = [date(2026, 9, 16) + timedelta(days=i) for i in range(4)]
    held = [bar(days[0], 105, 106, 99, 104), bar(days[1], 104, 105, 103, 104)]
    assert ls.score_level(D("100"), "support", held, tol) == "held"
    lost_later = held + [bar(days[2], 100, 100, 95, 98)]
    assert ls.score_level(D("100"), "support", lost_later, tol) == "lost"  # lost dominates
    inside_tol = held + [bar(days[2], 100, 100, 98, 99.5)]
    assert ls.score_level(D("100"), "support", inside_tol, tol) == "held"
    reached = [bar(days[0], 101, 102, 99.5, 99.5), bar(days[1], 99.5, 101, 99.2, 100.5)]
    assert ls.score_level(D("100"), "support", reached, tol) == "reached"  # closed under it, within tol
    next_through = [bar(days[0], 105, 106, 99, 101), bar(days[1], 101, 101, 97, 98)]
    assert ls.score_level(D("100"), "support", next_through, tol) == "lost"
    assert ls.score_level(D("100"), "support", [bar(days[0], 110, 111, 105, 108)], tol) == "expired"
    # resistance mirrored
    assert ls.score_level(D("100"), "resistance", [bar(days[0], 95, 101, 94, 96)], tol) == "held"
    assert ls.score_level(D("100"), "resistance", [bar(days[0], 95, 103, 94, 102)], tol) == "lost"
    assert ls.score_level(D("100"), "resistance", [], tol) == "expired"


def test_trigger_race_short_and_no_trigger() -> None:
    days = [date(2026, 9, 16) + timedelta(days=i) for i in range(3)]
    fwd = [bar(days[0], 52, 53, 49, 50), bar(days[1], 50, 51, 44, 45)]
    assert ls.trigger_outcome(D("50"), "short", [D("40"), D("45")], [D("60")], fwd) == ("target", "target 45")
    assert ls.trigger_outcome(D("50"), "long", [], [], [bar(days[0], 40, 41, 39, 40)])[0] == "no_trigger"
    neither = ls.trigger_outcome(D("50"), "long", [D("70")], [D("40")], [bar(days[0], 49, 51, 48, 50)])
    assert neither == ("neither", "target 70")


def test_tolerance_is_atr_scaled_with_a_floor(cfg: ScoutConfig) -> None:
    start = date(2026, 8, 1)
    quiet = [bar(start + timedelta(days=i), 100, 100.1, 99.9, 100) for i in range(20)]
    assert ls.tolerance_for(quiet, D("100"), cfg) == D("0.75")  # 0.75% floor beats 0.5 x 0.2
    wild = [bar(start + timedelta(days=i), 100, 106, 94, 100) for i in range(20)]
    assert ls.tolerance_for(wild, D("100"), cfg) == D("6")  # 0.5 x ATR 12
    assert ls.tolerance_for(wild[:10], D("100"), cfg) == D("0.75")  # too few bars for ATR14
    assert ls.atr(wild, 14) == D("12")


# --------------------------------------------------------------------------- fix 1: through the loader


def test_ranges_and_alternatives_expand_into_scored_readings(tmp_path: Path, cfg: ScoutConfig) -> None:
    folder = tmp_path / "levels"
    write_read(folder, plan_with([one_setup("A", [
        {"price": "130 to 125", "kind": "support"},
        {"price": "330s", "kind": "support"},  # refused by the loader, never scored
        {"price": "7.30", "kind": "resistance", "ambiguous": True, "price_alternatives": ["730"]}])]))
    bars = [bar("2026-09-15", 140, 141, 139, 140), bar("2026-09-16", 140, 141, 128, 135)]
    card = _card(folder, {"A": bars}, cfg)
    scored = sorted((r.kind, str(r.price), r.side, r.ambiguous) for r in card.levels)
    assert scored == [("resistance", "7.30", "support", True),  # below the price, so scored as a support
                      ("resistance", "730", "resistance", True),
                      ("support", "125", "support", True), ("support", "130", "support", True)]
    assert any("ambiguous" in n for n in card.notes)


# --------------------------------------------------------------------------- fix 2: own window


def test_each_setup_uses_its_own_expires(tmp_path: Path, cfg: ScoutConfig) -> None:
    folder = tmp_path / "levels"
    write_read(folder, plan_with([
        one_setup("SHORT", [{"price": "100", "kind": "support"}], expires="2026-09-16"),
        one_setup("LONG", [{"price": "100", "kind": "support"}], expires="2026-09-24")]))
    days = [date(2026, 9, 15) + timedelta(days=i) for i in range(10)]
    series = [bar(days[0], 105, 105, 105, 105)] + [bar(d, 105, 106, 104, 105) for d in days[1:3]] + \
             [bar(days[3], 101, 101, 95, 96)]  # breaks on 09-18
    card = _card(folder, {"SHORT": series, "LONG": series}, cfg, today=date(2026, 9, 28))
    by_symbol = {r.symbol: (r.outcome, r.sessions) for r in card.levels}
    assert by_symbol["SHORT"] == ("expired", 1)  # its window was 09-16 only
    assert by_symbol["LONG"] == ("lost", 3)


def test_an_open_window_is_not_scored(tmp_path: Path, cfg: ScoutConfig) -> None:
    folder = tmp_path / "levels"
    write_read(folder, plan_with([one_setup("A", [{"price": "100", "kind": "support"}])]))
    card = _card(folder, {"A": [bar("2026-09-15", 105, 105, 105, 105), bar("2026-09-16", 105, 105, 95, 96)]}, cfg,
                 today=date(2026, 9, 22))
    assert card.levels == [] and card.open_windows == 1


def test_a_months_horizon_gets_at_least_ten_sessions() -> None:
    setup = {"session_date": "2026-09-15", "expires": "2026-09-22", "horizon": "a few months"}
    assert ls.window_end(setup) == date(2026, 9, 29)  # ten trading days after Tuesday 09-15
    assert ls.window_end(dict(setup, horizon="this week")) == date(2026, 9, 22)
    assert ls.window_end(dict(setup, horizon="weeks", expires="2026-10-30")) == date(2026, 10, 30)


# --------------------------------------------------------------------------- fix 3: the null


NULL_SCRIPT = textwrap.dedent("""
    import json, sys
    from datetime import date, datetime, timedelta, timezone
    from decimal import Decimal
    from types import SimpleNamespace
    from scout.levels_bars import DailyBar
    from scout.levels_score import null_for
    start = date(2026, 3, 2)
    bars = [DailyBar(start + timedelta(days=i), Decimal(100 + i % 7), Decimal(103 + i % 7), Decimal(97 + i % 5),
                     Decimal(100 + i % 6)) for i in range(160)]
    cfg = SimpleNamespace(levels_null_window_bars=126, levels_null_draws=60, levels_tolerance_atr_mult=0.5,
                          levels_tolerance_floor_pct=0.0075)
    print(json.dumps(null_for(seed="scout-levels-v1", symbol="NVDA", session_date="2026-08-01", kind="support",
                              price=Decimal("101"), reference=Decimal("102"), history=bars[:150],
                              forward=bars[150:], cfg=cfg)))
""")


def test_the_null_is_identical_across_processes() -> None:
    root = Path(__file__).resolve().parent.parent
    outputs = []
    for hash_seed in ("1", "2", "random"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        done = subprocess.run([sys.executable, "-c", NULL_SCRIPT], cwd=root, env=env, capture_output=True,
                              text=True, timeout=60, check=True)
        outputs.append(json.loads(done.stdout))
    assert outputs[0] == outputs[1] == outputs[2]
    assert outputs[0][1] > 0  # it scored something
    namespace: dict = {}
    exec(compile(NULL_SCRIPT.replace("print(", "result = ("), "<null>", "exec"), namespace)  # noqa: S102
    assert json.loads(namespace["result"]) == outputs[0]


def test_null_seed_is_sha256_and_per_level() -> None:
    a = ls.null_seed("s", "NVDA", "2026-09-15", "support", D("213"))
    assert a == ls.null_seed("s", "NVDA", "2026-09-15", "support", "213")
    assert a != ls.null_seed("s", "NVDA", "2026-09-15", "support", D("190"))
    assert a != ls.null_seed("t", "NVDA", "2026-09-15", "support", D("213"))


def test_a_rate_needs_its_null() -> None:
    with pytest.raises(ls.NullRequired):
        ls.format_rate("x", 1, 2, None, 3, min_scored=20)
    assert "n/a" in ls.format_rate("x", 0, 0, 0, 0, min_scored=20)
    line = ls.format_rate("x", 15, 20, 30, 60, min_scored=20)
    assert "75% (15/20, n=20) vs null 50% (n=60), lift 1.50x" in line and "INDICATIVE" not in line
    assert "INDICATIVE (n=19 < 20)" in ls.format_rate("x", 10, 19, 30, 60, min_scored=20)
    assert "null not computed" in ls.format_rate("x", 1, 2, 0, 0, min_scored=20)


def test_min_scored_comes_from_config(tmp_path: Path, scout_dir: Path) -> None:
    from conftest import make_config

    cfg = make_config(scout_dir, levels_score_min_scored=1)
    folder = tmp_path / "levels"
    write_read(folder, plan_with([one_setup("A", [{"price": "100", "kind": "support"}])]))
    card = _card(folder, {"A": [bar("2026-09-15", 105, 105, 105, 105), bar("2026-09-16", 104, 104, 99, 104)]}, cfg)
    assert card.min_scored == 1
    assert "all levels HELD: 100% (1/1, n=1)" in ls.render(card)
    assert "INDICATIVE (n=1" not in ls.render(card)


# --------------------------------------------------------------------------- loaders


RADAR_BARS_SQL = ("CREATE TABLE bars (symbol TEXT NOT NULL, minutes INTEGER NOT NULL, start TEXT NOT NULL, "
                  "trading_day TEXT NOT NULL, open TEXT NOT NULL, high TEXT NOT NULL, low TEXT NOT NULL, "
                  "close TEXT NOT NULL, volume TEXT NOT NULL, session TEXT, fetched_at TEXT NOT NULL, "
                  "PRIMARY KEY (symbol, minutes, start))")


def _radar(tmp_path: Path, rows: list[tuple]) -> Path:
    radar = tmp_path / "Radar"
    (radar / "state").mkdir(parents=True)
    conn = sqlite3.connect(radar / "state" / "radar.db")
    conn.execute(RADAR_BARS_SQL)
    conn.executemany("INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return radar


def _row(symbol: str, minutes: int, day: str, o: str, h: str, lo: str, c: str) -> tuple:
    return (symbol, minutes, f"{day}T13:30:00Z", day, o, h, lo, c, "1000", "CORE", "2026-09-20T00:00:00Z")


def test_radar_bars_reads_daily_rows_read_only(tmp_path: Path) -> None:
    radar = _radar(tmp_path, [_row("NVDA", DAY_MINUTES, "2026-09-16", "215", "222", "212", "221"),
                              _row("NVDA", 1, "2026-09-16", "1", "1", "1", "1"),
                              _row("NVDA", DAY_MINUTES, "2026-09-15", "215", "216", "214", "215"),
                              _row("NVDA", DAY_MINUTES, "2026-09-17", "x", "1", "1", "1")])
    db = radar / "state" / "radar.db"
    before = (db.read_bytes(), db.stat().st_mtime_ns, sorted(p.name for p in db.parent.iterdir()))
    loader = RadarBars(radar)
    assert loader.check() is None
    bars = loader.daily("nvda", date(2026, 9, 1), date(2026, 9, 30))
    assert [(b.day.isoformat(), str(b.close)) for b in bars] == [("2026-09-15", "215"), ("2026-09-16", "221")]
    assert loader.daily("../x", date(2026, 9, 1), date(2026, 9, 30)) == []
    with pytest.raises(sqlite3.OperationalError):
        conn = loader._connect()
        try:
            conn.execute("DELETE FROM bars")
        finally:
            conn.close()
    assert (db.read_bytes(), db.stat().st_mtime_ns, sorted(p.name for p in db.parent.iterdir())) == before


def test_radar_bars_says_no_daily_bars(tmp_path: Path) -> None:
    radar = _radar(tmp_path, [_row("NVDA", 1, "2026-09-16", "1", "1", "1", "1")])
    loader = RadarBars(radar)
    assert loader.check() == "no daily bars: Radar's database holds no daily (1440-minute) bars"
    missing = RadarBars(tmp_path / "NoRadar")
    assert "could not be opened" in (missing.check() or "")
    assert missing.daily("NVDA", date(2026, 9, 1), date(2026, 9, 30)) == []


def test_csv_bars(tmp_path: Path) -> None:
    folder = tmp_path / "csv"
    folder.mkdir()
    (folder / "NVDA.csv").write_text("Date,Open,High,Low,Close,Volume\n2026-09-16,215,222,212,221,9\n"
                                     "2026-09-15,215,216,214,215,9\nbad,1,1,1,1,1\n2026-09-17,1,0.5,2,1,1\n",
                                     encoding="utf-8")
    (folder / "BAD.csv").write_text("day,price\n2026-09-16,1\n", encoding="utf-8")
    loader = CsvBars(folder)
    assert loader.check() is None
    assert [b.day.isoformat() for b in loader.daily("NVDA", date(2026, 9, 1), date(2026, 9, 30))] == [
        "2026-09-15", "2026-09-16"]
    assert loader.daily("NVDA", date(2026, 9, 16), date(2026, 9, 16))[0].high == D("222")
    assert loader.daily("BAD", date(2026, 9, 1), date(2026, 9, 30)) == [] and loader.notes
    assert loader.daily("NONE", date(2026, 9, 1), date(2026, 9, 30)) == []
    assert CsvBars(tmp_path / "absent").check()


# --------------------------------------------------------------------------- the command


def _ctx(cfg: ScoutConfig, clock: ManualClock) -> SimpleNamespace:
    return SimpleNamespace(cfg=cfg, paths=ScoutPaths.from_config(cfg), clock=clock,
                           log=logging.getLogger("test.levels_score"))


def test_cli_writes_the_report_from_radar_bars(tmp_path: Path, scout_dir: Path, write_audit) -> None:
    from conftest import make_config

    radar = _radar(tmp_path, [_row("NVDA", DAY_MINUTES, "2026-09-15", "215", "216", "214", "215"),
                              _row("NVDA", DAY_MINUTES, "2026-09-16", "215", "222", "212", "221")])
    cfg = make_config(scout_dir, radar_dir=str(radar))
    ctx = _ctx(cfg, ManualClock(datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)))
    write_read(ctx.paths.levels, plan_with([one_setup("NVDA", [{"price": "213", "kind": "support"}])]))
    with write_audit.watching(scout_dir):
        code = ls.cli_levels_score(ctx, argparse.Namespace(bars="radar", csv_dir=None, since=None))
    assert code == 0
    report = (scout_dir / "reports" / "levels-score-2026-09-28.md").read_text(encoding="utf-8")
    assert "Bars: radar" in report and "| support | NVDA | 213 | support |" in report


def test_cli_bad_input(scout_dir: Path, cfg: ScoutConfig, clock: ManualClock) -> None:
    ctx = _ctx(cfg, clock)
    assert ls.cli_levels_score(ctx, argparse.Namespace(bars="radar", csv_dir=None, since=None)) == 2
    assert ls.cli_levels_score(ctx, argparse.Namespace(bars="csv", csv_dir=None, since=None)) == 2
    assert ls.cli_levels_score(ctx, argparse.Namespace(bars="csv", csv_dir="x", since="soon")) == 2


def test_cli_csv_with_since(tmp_path: Path, cfg: ScoutConfig, clock: ManualClock) -> None:
    ctx = _ctx(cfg, clock)
    write_read(ctx.paths.levels, plan_with([one_setup("A", [{"price": "1", "kind": "support"}])]))
    folder = tmp_path / "csv"
    folder.mkdir()
    code = ls.cli_levels_score(ctx, argparse.Namespace(bars="csv", csv_dir=str(folder), since="2026-10-01"))
    assert code == 0
    report = (ctx.paths.reports / "levels-score-2026-11-02.md").read_text(encoding="utf-8")
    assert "Level files: 0" in report
