"""Paper Gains scanner alerts for one symbol (DESIGN 2.7, 3.1; for Radar's later tags, SCOUT_SPEC 9.1).

Descriptive only, stdlib only, never raises. Scout stores one `scanner` fact per ticker of each
#15m-scanner, #daily-scanner and #weekly-scanner card ("Outside Bar - 15m (In Force)": timeframe,
pattern and state as printed). No gate reads them; this module only lists them, so that Radar can
later tag a find with "on a Paper Gains scanner" through its own spec change.

`scanner_alerts_from(facts, beat, symbol, as_of, start)`:
- `true` when a visible scanner fact (the as-of rule: `first_seen_at` and `published_at` at or
  before `as_of`) names `symbol` with `published_at` in [start, as_of]; `alerts` lists them newest
  first (at most `MAX_ALERTS`), each {timeframe, pattern, state, posted_at, channel, fact_id};
- `false` when none does and `discord_inbox` is `ok` in the governing beat;
- `unknown` when it is not (`source_not_ok:discord_inbox`: the captures may be behind), so a missing
  capture never reads as "not on a scanner". A `true` stays `true`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from scout_reader import records as R

__all__ = ["KIND", "MAX_ALERTS", "SOURCE", "TIMEFRAMES", "scanner_alerts_from"]

KIND = "scanner"
SOURCE = "discord_inbox"
TIMEFRAMES = ("15m", "daily", "weekly")
MAX_ALERTS = 50


def _alert(rec: Mapping[str, Any], symbol: str, as_of: datetime, start: datetime) -> dict[str, Any] | None:
    if not isinstance(rec, Mapping) or rec.get("kind") != KIND or not R.visible(rec, as_of):
        return None
    ticker = rec.get("ticker")
    if not isinstance(ticker, str) or ticker.strip().upper() != symbol:
        return None
    posted = R.parse_time(rec.get("published_at"))
    extra = rec.get("extra")
    if posted is None or not start <= posted <= as_of or not isinstance(extra, Mapping):
        return None
    tf, pattern, state = extra.get("timeframe"), extra.get("pattern"), extra.get("state")
    if tf not in TIMEFRAMES or not isinstance(pattern, str):
        return None
    return {"timeframe": tf, "pattern": pattern, "state": state if isinstance(state, str) else None,
            "posted_at": rec.get("published_at"), "channel": str(rec.get("feed")), "fact_id": str(rec.get("fact_id"))}


def scanner_alerts_from(facts: Iterable[Mapping[str, Any]], beat: Mapping[str, Any] | None, symbol: str,
                        as_of: datetime, start: datetime) -> dict[str, Any]:
    try:
        alerts = []
        for rec in facts:
            try:
                got = _alert(rec, symbol, as_of, start)
            except (TypeError, ValueError, OverflowError):
                got = None
            if got is not None:
                alerts.append(got)
        alerts.sort(key=lambda a: (str(a["posted_at"]), a["fact_id"]), reverse=True)
        alerts = alerts[:MAX_ALERTS]
        if alerts:
            res = R.result("true", [f"discord:{a['fact_id']}" for a in alerts], False, ("scanner_alert",))
        else:
            sources = (beat or {}).get("sources")
            status = sources.get(SOURCE) if isinstance(sources, Mapping) else None
            res = (R.result("false", (), False, ("no_scanner_alert",)) if status == "ok"
                   else R.result("unknown", (), False, (f"source_not_ok:{SOURCE}",)))
        res["alerts"] = alerts
        return res
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - never raises into the caller
        return dict(R.result("unknown", (), False, (f"error:{type(exc).__name__}",)), alerts=[])
