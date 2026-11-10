"""`python -m scout reads market` / `Check Market.bat` (0.3.2; build plan amendment E): it says whether
Alpaca accepted the key, the newest prices with their age, and what a reader would see; it never
prints the key and writes no record or state, only its log lines. A fake Alpaca service; no network,
no real key."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_EMAIL
from helpers_fakes import FakeResponse
from helpers_market import (
    FAKE_ID, FAKE_SECRET, FakeAlpaca, asked_end, closes_answer, delayed_answer, live_answer, ok,
)
from test_market_feed import WED, et

from scout import cli
from scout.clock import ManualClock
from scout.logs import close_logging
from scout.market_feed import DELAYED, LIVE
from scout.paths import ScoutPaths
from scout.reads_file import iso_us
from scout.reads_market import market_file
from scout.store import append_jsonl


def _folder(scout_dir: Path, *, env: bool = True, **settings: Any) -> Path:
    lines = [f'contact_email = "{TEST_EMAIL}"', "reads_enabled = true"]
    lines += [f"{k} = {str(v).lower() if isinstance(v, bool) else repr(v)}" for k, v in settings.items()]
    config = scout_dir / "config.toml"
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if env:
        (scout_dir / ".env").write_text(f"APCA_API_KEY_ID={FAKE_ID}\nAPCA_API_SECRET_KEY={FAKE_SECRET}\n",
                                        encoding="utf-8")
    return config


def _snapshot(scout_dir: Path) -> list[tuple[str, int, int]]:
    return sorted((str(p.relative_to(scout_dir)), p.stat().st_size, p.stat().st_mtime_ns)
                  for p in scout_dir.rglob("*") if p.is_file() and "logs" not in p.relative_to(scout_dir).parts)


def _check(scout_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], when: datetime,
           **routes: Any) -> tuple[int, str, FakeAlpaca]:
    clock = ManualClock(when)
    fake = FakeAlpaca(clock, **routes)
    monkeypatch.setattr("scout.app.SystemClock", lambda: clock)
    monkeypatch.setattr("scout.http.urllib_transport", fake)
    try:
        rc = cli.main(["--config", str(scout_dir / "config.toml"), "reads", "market"])
    finally:
        close_logging()
    return rc, capsys.readouterr().out, fake


def _logs(scout_dir: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in (scout_dir / "logs").glob("*.log"))


def test_reads_market_prints_line_age_feed_and_writes_nothing(scout_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    _folder(scout_dir)
    paths = ScoutPaths(scout_dir)
    now = et(WED, "10:02")
    seen = now - timedelta(seconds=40)
    syms = {s: {"last": p, "change_pct": c, "prev_close": "1", "trade_at": iso_us(seen)}
            for s, p, c in (("SPY", "681.2", "0.003094"), ("QQQ", "611.05", "0.004201"), ("IWM", "241.13", "-0.002152"))}
    append_jsonl(paths, market_file(paths, WED), {"format": 1, "kind": "market", "first_seen_at": iso_us(seen),
                                                  "at": iso_us(seen), "feed": "iex", "symbols": syms})
    before = _snapshot(scout_dir)
    rc, out, fake = _check(scout_dir, monkeypatch, capsys, now)
    assert rc == 0, out
    assert fake.legs() == [LIVE, DELAYED, "closes"]
    lines = out.splitlines()
    assert lines[0] == "Alpaca market data (data.alpaca.markets), with the key in Scout's .env:"
    assert lines[1] == "- Live prices (IEX exchange only): answered."
    assert lines[2] == "- Whole-market prices (at least 20 minutes old): answered."
    assert lines[3] == "- Previous closes (whole market, 2026-11-10): answered."
    assert lines[4].startswith("Newest prices: SPY 681.2 (+0.31%); QQQ 611.05 (+0.42%); IWM 241.13 (-0.22%), at ")
    assert "seconds old (live IEX prices); change from the whole market's close on 2026-11-10." in lines[4]
    # the whole-market request's own newest prices too, with their age
    assert lines[5].startswith("Newest prices: SPY 681.2 (+0.31%); QQQ 611.05 (+0.42%); IWM 241.13 (-0.22%), at "
                               "09:42:00 ET on 2026-11-11, 20 minutes old (whole-market prices, delayed)")
    assert lines[6] == ("A reader now would see: Market at 10:01:20 ET: SPY 681.20 +0.31%; QQQ 611.05 +0.42%; "
                        "IWM 241.13 -0.22%")
    assert lines[7:] == ["The key works: Alpaca accepted it and answered."]  # the closing line
    assert _snapshot(scout_dir) == before  # no record, miss line, raw file or host state
    for value in (FAKE_ID, FAKE_SECRET):
        assert value not in out and value not in _logs(scout_dir)


def test_closed_hours_say_the_prices_are_old_but_the_key_works(scout_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                                capsys: pytest.CaptureFixture[str]) -> None:
    _folder(scout_dir)
    sat = et(date(2026, 11, 14), "11:00")
    rc, out, _fake = _check(scout_dir, monkeypatch, capsys, sat,
                            **{LIVE: lambda url, now: live_answer(et(date(2026, 11, 13), "16:59")),
                               DELAYED: lambda url, now: live_answer(now)},  # not bars: a changed shape
                            prev_day=date(2026, 11, 12))
    assert rc == 0, out
    assert out.splitlines()[-1] == ("The key works: Alpaca accepted it and answered. One answer was not in the shape "
                                    "Scout knows: copy these lines to Claude.")
    assert "hours" in out
    assert "The market is closed now, so these prices are hours old; the key works all the same." in out
    assert "Whole-market prices (at least 20 minutes old): answered, but not in the shape Scout knows." in out
    assert "A reader now would see: Market: not available" in out


def test_reads_market_failure_is_plain_and_keyless(scout_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    _folder(scout_dir)
    refused = FakeResponse(403, {}, b'{"message":"forbidden"}')
    before = _snapshot(scout_dir)
    rc, out, fake = _check(scout_dir, monkeypatch, capsys, et(WED, "10:02"),
                           **{LIVE: refused, DELAYED: refused, "closes": refused})
    assert rc == 1
    assert "- Live prices (IEX exchange only): answered HTTP 403 (the key was refused)." in out
    assert "Alpaca refused the key: check the two lines in Scout's .env." in out
    assert fake.legs() == [LIVE]  # the 403 backs the host off: nothing more is asked
    assert _snapshot(scout_dir) == before
    for value in (FAKE_ID, FAKE_SECRET):
        assert value not in out and value not in _logs(scout_dir)


@pytest.mark.parametrize("settings, env, words", [
    ({}, False, "No Alpaca key: add two lines to Scout's .env"),
    ({"reads_market_enabled": False}, True, "The market feed is off (reads_market_enabled = false"),
    ({"contact_email": ""}, True, "contact_email is empty"),
])
def test_not_set_up_exits_2(scout_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                            settings: dict[str, Any], env: bool, words: str) -> None:
    config = _folder(scout_dir, env=env, **{k: v for k, v in settings.items() if k != "contact_email"})
    if "contact_email" in settings:
        config.write_text(config.read_text(encoding="utf-8").replace(TEST_EMAIL, ""), encoding="utf-8")
    rc, out, fake = _check(scout_dir, monkeypatch, capsys, et(WED, "10:02"))
    assert rc == 2 and words in out and fake.calls == []
    if not env:
        assert "APCA_API_KEY_ID" in out and "ALPACA_SECRET_KEY" in out


# --------------------------------------------------------------------------- old prices are not a failure (review L2)

TUE = date(2026, 11, 10)
RAW_CODES = ("no_recent_bar", "missing_symbol", "no_prev_close", "future_time", ": old", "(old", "stale",
             "refused_key", "http_")


def test_pre_market_old_prices_still_confirm_the_key(scout_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    """05:00 ET (19:00 in Brisbane): IEX's last trade is yesterday's and IWM has no whole-market bar yet. The
    key works: exit 0, the newest prices each request returned with their age, plain words, no codes."""
    _folder(scout_dir)
    rc, out, _fake = _check(scout_dir, monkeypatch, capsys, et(WED, "05:00"), prev_day=TUE,
                            **{LIVE: lambda url, now: live_answer(et(TUE, "16:59")),
                               DELAYED: lambda url, now: delayed_answer(asked_end(url), skip=("IWM",))})
    assert rc == 0, out
    lines = out.splitlines()
    live = next(line for line in lines if "(live IEX prices)" in line)
    assert live.startswith("Newest prices: SPY 681.2 (+0.31%); QQQ 611.05 (+0.42%); IWM 241.13 (-0.22%), at "
                           "16:58:58 ET on 2026-11-10, 12 hours 1 minute old (live IEX prices)")
    assert live.endswith("Not used now: they are from before today's 04:00 ET start.")
    delayed = next(line for line in lines if "(whole-market prices, delayed)" in line)
    assert "SPY 681.2 (+0.31%); QQQ 611.05 (+0.42%); IWM no price, at 04:40:00 ET on 2026-11-11, 20 minutes old" in delayed
    assert ("Not used now: Alpaca had no whole-market 1-minute bar for every symbol 20 to 35 minutes ago (few "
            "trades happen early in the morning).") in delayed
    assert "This is normal before about 08:00 ET." in out  # the pre-market only
    assert lines[-2].startswith("A reader now would see: Market: not available")
    assert lines[-1] == "The key works: Alpaca accepted it and answered."
    assert not [c for c in RAW_CODES if c in out], out


def test_at_night_the_old_prices_confirm_the_key(scout_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    _folder(scout_dir)
    rc, out, _fake = _check(scout_dir, monkeypatch, capsys, et(WED, "03:00"), prev_day=date(2026, 11, 9),
                            **{LIVE: lambda url, now: live_answer(et(TUE, "16:59")),
                               DELAYED: lambda url, now: ok({"bars": {}, "next_page_token": None})})
    assert rc == 0, out
    assert "The market is closed now, so these prices are hours old; the key works all the same." in out
    assert "(live IEX prices)" in out and "normal before about 08:00" not in out
    assert ("Whole-market prices: no prices, because Alpaca had no whole-market 1-minute bar for every symbol 20 to "
            "35 minutes ago (the market is closed).") in out and "early in the morning" not in out
    assert out.splitlines()[-1] == "The key works: Alpaca accepted it and answered."
    assert not [c for c in RAW_CODES if c in out], out


def test_in_session_old_prices_say_so_without_the_early_hours_note(scout_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    _folder(scout_dir)
    rc, out, _fake = _check(scout_dir, monkeypatch, capsys, et(WED, "11:00"),
                            **{LIVE: lambda url, now: live_answer(now - timedelta(minutes=40)),
                               DELAYED: lambda url, now: delayed_answer(now - timedelta(minutes=45))})
    assert rc == 0, out
    assert "40 minutes old (live IEX prices)" in out and "45 minutes old (whole-market prices, delayed)" in out
    assert "Prices this old (over 30 minutes, reads_market_max_age_s, or from before today's 04:00 ET) are not shown "\
           "to a reader." in out
    assert "normal before about 08:00" not in out
    assert out.splitlines()[-1] == "The key works: Alpaca accepted it and answered."


def test_one_refused_request_beside_an_answer_is_not_called_a_refused_key(scout_dir: Path,
                                                                           monkeypatch: pytest.MonkeyPatch,
                                                                           capsys: pytest.CaptureFixture[str]) -> None:
    """05:00 ET: Alpaca refuses the whole-market request (its real 403 body) but answers the live one. The
    key works, and no line may say it was refused; the check says what the running Scout does about it."""
    _folder(scout_dir)
    refused = FakeResponse(403, {}, b'{"message": "subscription does not permit querying recent SIP data"}')
    rc, out, _fake = _check(scout_dir, monkeypatch, capsys, et(WED, "05:00"), prev_day=TUE,
                            **{LIVE: lambda url, now: live_answer(et(TUE, "16:59")), DELAYED: refused})
    assert rc == 0, out
    assert "the key was refused" not in out and "refused the key" not in out
    assert "- Whole-market prices (at least 20 minutes old): answered HTTP 403 (Alpaca refused this request)." in out
    assert ("Alpaca refused the request for whole-market prices but answered another, so the key itself works. The "
            "running Scout leaves a refused request out until the next day and carries on with the rest.") in out
    assert out.splitlines()[-1] == ("The key works: Alpaca accepted it and answered. If the check still says in a few "
                                    "minutes that no prices would be recorded, copy these lines to Claude.")
    assert not [c for c in RAW_CODES if c in out], out


def test_exit_1_when_every_answer_is_unreadable(scout_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    _folder(scout_dir)
    garbage = FakeResponse(200, {}, b"{not json")
    rc, out, fake = _check(scout_dir, monkeypatch, capsys, et(WED, "10:02"),
                           **{LIVE: garbage, DELAYED: ok([1, 2]), "closes": garbage})
    assert fake.legs() == [LIVE, DELAYED, "closes"]
    assert rc == 1, out
    assert out.splitlines()[-1] == ("Alpaca answered, but not in the shape Scout knows, so the key could not be "
                                    "confirmed: copy these lines to Claude.")


def test_no_answer_at_all_exits_1(scout_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    _folder(scout_dir)
    rc, out, fake = _check(scout_dir, monkeypatch, capsys, et(WED, "10:02"), **{LIVE: OSError("reset")})
    assert rc == 1 and fake.legs() == [LIVE]  # a network failure ends the poll
    assert "- Whole-market prices (at least 20 minutes old): not asked (an earlier request got no answer" in out
    assert out.splitlines()[-1] == "Alpaca gave no answer this time, so the key could not be checked: try again in a minute."


def test_odd_numbers_never_raise(scout_dir: Path, monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    """An absurd price is a changed shape; the most extreme price Scout accepts against the smallest close
    still prints a change. The check never raises."""
    _folder(scout_dir)
    huge = ok({"SPY": {"latestTrade": {"t": "2026-11-11T15:01:00Z", "p": "#1e300"}}})
    extreme = {"SPY": "#10000000", "QQQ": "#10000000", "IWM": "#10000000"}
    tiny = {"SPY": "#0.0001", "QQQ": "#0.0001", "IWM": "#0.0001"}
    rc, out, _fake = _check(scout_dir, monkeypatch, capsys, et(WED, "10:02"),
                            **{LIVE: huge, DELAYED: lambda url, now: delayed_answer(asked_end(url), extreme),
                               "closes": closes_answer(TUE, tiny)})
    assert rc == 0, out
    assert "- Live prices (IEX exchange only): answered, but not in the shape Scout knows." in out
    assert "SPY 10000000 (+9999999999900.00%)" in out
    assert out.splitlines()[-1] == ("The key works: Alpaca accepted it and answered. One answer was not in the shape "
                                    "Scout knows: copy these lines to Claude.")
