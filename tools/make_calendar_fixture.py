"""Write tests/fixtures/guardian_calendar_2026_2027.json from Trade Guardian's own clock.py.

Run once, by hand, with Guardian's own Python, read-only (DESIGN 1, SS 3 "a shared fixture test"):

    E:\\Trade_Guardian_dev\\.venv\\Scripts\\python.exe -B tools\\make_calendar_fixture.py ^
        E:\\Trade_Guardian_dev tests\\fixtures\\guardian_calendar_2026_2027.json

`-B` keeps Python from writing __pycache__ into Guardian's folder. The script only imports
`trade_guardian.clock` and writes the one output file named on the command line (inside Scout's
folder). Scout's test (tests/test_calendar.py) compares its copy against this file, so Scout never
needs Trade Guardian installed (SS 2.1).

For every day of 2026-2027 it records Guardian's calendar answers, and for a fixed set of ET wall
times on that day the UTC instant, Guardian's session and Guardian's trading day.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import date, time, timedelta, timezone
from pathlib import Path

# ET wall times probed every day (both sides of every session boundary, plus night hours)
PROBE_TIMES = ("00:30", "03:59", "04:00", "09:29", "09:30", "12:59", "13:00", "15:59", "16:00",
               "16:59", "17:00", "19:59", "20:00", "23:30")


def _iso(dt) -> str:  # noqa: ANN001
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: make_calendar_fixture.py <guardian_repo> <output.json>")
        return 2
    repo, out = Path(argv[0]).resolve(), Path(argv[1]).resolve()
    sys.path.insert(0, str(repo))
    from trade_guardian import clock as g  # noqa: PLC0415 - imported from the repo given

    clock_file = repo / "trade_guardian" / "clock.py"
    commit = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%H", "--", "trade_guardian/clock.py"],
                            capture_output=True, text=True, check=True).stdout.strip()
    days = []
    day = date(2026, 1, 1)
    while day <= date(2027, 12, 31):
        st = g.session_times(day)
        probes = []
        for text in PROBE_TIMES:
            hh, mm = (int(x) for x in text.split(":"))
            instant = g.et_datetime(day, time(hh, mm))
            probes.append({"et": text, "utc": _iso(instant), "session": str(g.session_at(instant).value),
                           "trading_day": g.trading_day_of(instant).isoformat()})
        days.append({
            "day": day.isoformat(),
            "calendar_covers": g.calendar_covers(day),
            "is_holiday": g.is_holiday(day),
            "is_early_close": g.is_early_close(day),
            "is_trading_day": g.is_trading_day(day),
            "next_trading_day": g.next_trading_day(day).isoformat(),
            "previous_trading_day": g.previous_trading_day(day).isoformat(),
            "session_times": None if st is None else {
                "pre_open": _iso(st.pre_open), "core_open": _iso(st.core_open),
                "core_close": _iso(st.core_close), "post_close": _iso(st.post_close),
                "early_close": st.early_close},
            "probes": probes,
        })
        day += timedelta(days=1)
    body = {
        "_about": "Trade Guardian's clock.py answers for every day of 2026-2027 (tools/make_calendar_fixture.py)",
        "guardian_commit": commit,
        "guardian_clock_sha256": hashlib.sha256(clock_file.read_bytes()).hexdigest(),
        "calendar_years": sorted(g.CALENDAR_YEARS),
        "days": days,
    }
    out.write_text(json.dumps(body, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(days)} days to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
