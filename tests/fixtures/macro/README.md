# Macro fixtures (unit macro)

All files here are **synthetic** (DESIGN D11), hand-built on 2026-11-01 from the shapes in
SCOUT_SPEC 4.1 #7-10 and 7.3; no network was used. Every name ends in `_SYNTHETIC`; JSON files also
carry a `_SYNTHETIC` key and `.ics` files an `X-SCOUT-NOTE` line (the Cboe CSV cannot carry a marker,
since its header is pinned). The lead replaces them with live captures at build step 2 (nowflation's
files are for private tests only and are never republished; credit "Nowflation",
https://nowflation.com).

| File | What it is |
|---|---|
| release_calendar_SYNTHETIC.json | `generated_at, from, source, events, past, fomc`; CPI with prior, nowflation_forecast, cleveland; NFP with prior, street; PCE with prior, kalshi; a `datetime_et` text that is never read |
| release_calendar_stale_SYNTHETIC.json | `generated_at` three days old |
| release_calendar_drift_SYNTHETIC.json | an event without `datetime_utc` (schema drift: the source is refused) |
| realtime_SYNTHETIC.json | ok; `change_20d` 0.31 (hot at the 0.25 default) |
| realtime_{run_not_ok,checks_not_ok,not_live,last_good_held,stale_weight,stale}_SYNTHETIC.json | one SCOUT_SPEC 7.3 fail-closed condition each |
| realtime_drift_SYNTHETIC.json, realtime_drift_type_SYNTHETIC.json | `run` missing; `realtime_yoy_pct` a string |
| forecasts_index_SYNTHETIC.json, forecasts_index_drift_SYNTHETIC.json | a `forecasts` list with two graded prints; drift: `forecasts` not a list |
| nowflation_SYNTHETIC.ics | the `.ics` fallback (UTC times) |
| bls_SYNTHETIC.ics | BLS schedule: no DTSTAMP, CRLF, a folded SUMMARY, `TZID=US-Eastern` and `America/New_York`, a PPI event that is left out |
| VIX_History_head_SYNTHETIC.csv | Cboe header `DATE,OPEN,HIGH,LOW,CLOSE` and a few rows, the last 10/30/2026 |

The shapes of `realtime.json` (`run`, `today`, `last_good_held`, `freshness`, `change_<n>d`) and
`forecasts/index.json` (`forecasts`, `graded`) are this unit's reading of SCOUT_SPEC 7.3, not yet
checked against a live read; `scout/nowflation.py` pins them and must follow the live capture.
