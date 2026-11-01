"""scout_reader._et agrees with zoneinfo (DESIGN D2) for every hour of 2026-2027, and wall times in
the DST gap and repeat resolve as zoneinfo does with fold=0."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from scout_reader._et import EDT, EST, dst_bounds_utc, et_to_utc, utc_to_et

NY = ZoneInfo("America/New_York")  # tzdata (dev extra) on Windows


def test_every_hour_of_2026_2027_matches_zoneinfo() -> None:
    at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2028, 1, 1, tzinfo=timezone.utc)
    bad = []
    while at < end:
        mine, theirs = utc_to_et(at), at.astimezone(NY)
        if mine.replace(tzinfo=None) != theirs.replace(tzinfo=None) or mine.utcoffset() != theirs.utcoffset():
            bad.append(at)
        at += timedelta(hours=1)
    assert bad == []


def test_wall_times_back_to_utc_match_zoneinfo_fold0() -> None:
    wall = datetime(2026, 1, 1)
    bad = []
    while wall < datetime(2028, 1, 1):
        expected = wall.replace(tzinfo=NY, fold=0).astimezone(timezone.utc)
        if et_to_utc(wall) != expected:
            bad.append(wall)
        wall += timedelta(minutes=30)
    assert bad == []


def test_dst_bounds_2026() -> None:
    start, end = dst_bounds_utc(2026)
    assert start == datetime(2026, 3, 8, 7, tzinfo=timezone.utc)
    assert end == datetime(2026, 11, 1, 6, tzinfo=timezone.utc)
    assert utc_to_et(start - timedelta(seconds=1)).tzinfo is EST and utc_to_et(start).tzinfo is EDT
    assert utc_to_et(end - timedelta(seconds=1)).tzinfo is EDT and utc_to_et(end).tzinfo is EST


def test_naive_input_refused() -> None:
    with pytest.raises(ValueError):
        utc_to_et(datetime(2026, 11, 2, 14))
