# Macro fixtures (unit macro)

## nowflation fixtures: synthetic, in the live layout

nowflation's terms forbid redistributing its raw files, so **no nowflation file in this repo is a
live capture or derived from one**. The `realtime.json`, `forecasts/index.json` and live-layout
calendar fixtures below are **synthetic files in nowflation's live layout as read on 2026-10-31**:
the same key names and JSON structure the parser in `scout/nowflation.py` reads, but every value is
invented (numbers, component names such as `component_a`, and free text, which is just
"synthetic"). Each keeps only the keys the parser and tests need, plus a few unknown keys
(`unknown_*`) that prove extra keys are tolerated, and carries a `_SYNTHETIC` key. Live captures stay
only in the owner's `E:\Scout\state\raw` (under `nowflation\`), never in the repo. Credit wherever
nowflation data appears: "Nowflation", https://nowflation.com.

| File | What it is |
|---|---|
| realtime_SYNTHETIC.json | `realtime.json`, generated Mon 2026-11-02 11:40Z, method `v9.1` (switched from `v9.0` on 2026-09-30). Headline 2.64; changes 1d +0.015 (`realtime_dod_pp`), 5d -0.072 (`realtime_wow_pp`), 20d -0.064 from `history` (11-02 2.637 minus 10-05 2.701, same method): regime "steady". Live 52.0, stale 4.0 (`component_a`): 7.1 % of the 56.0 market-sourced weight. `history` has 10 points: 09-29 (`v9.0`), 09-30, 10-03..10-07 (the 26..30-day window), 10-26, 11-01, 11-02 |
| realtime_{run_not_ok,checks_not_ok,not_live,last_good_held,stale}_SYNTHETIC.json | realtime_SYNTHETIC with one SCOUT_SPEC 7.3 fail-closed condition each: `run.ok` false, `run.checks_ok` false, `today.record_type` "reconstructed", `today.last_good_held` true, `generated_at` three days old |
| realtime_stale_weight_SYNTHETIC.json | live 44.0, stale 12.0 (components a, b, c): 21.4 % of the market-sourced weight, over the 20 % default |
| realtime_all_stale_SYNTHETIC.json | every market-sourced component stale (components a-e): `live_share_pct` 0, `stale_weight_pct` 56.0 (100 %) |
| realtime_drift_SYNTHETIC.json, realtime_drift_type_SYNTHETIC.json | schema drift: `run` missing; `today.realtime_yoy_pct` a string |
| forecasts_index_SYNTHETIC.json | `forecasts/index.json`: `n_graded` 3, `latest` 2026-10, four receipts (2026-10 provisional: forecast 2.71, Cleveland 2.58, no street or Kalshi; 2026-09 locked: forecast 2.83, street 2.8, Cleveland 2.86, actual 2.9, error -0.07; 2026-08 and 2026-07 locked) |
| forecasts_index_drift_SYNTHETIC.json | schema drift: `receipts` an object |
| release_calendar_live_layout_SYNTHETIC.json | `release_calendar.json` in the live layout (`date`, `data_month`, `unit`, `summary`, FOMC entries with `datetime_utc`); the October CPI event has a prior (2.9) only, so the lean takes the forecast and Cleveland value from the 2026-10 receipt |

They were written by a one-off script (no network, no live file read); edit them by hand or
regenerate them the same way.

## Synthetic files

The remaining files here are **synthetic** (DESIGN D11), hand-built on 2026-11-01 from the shapes in
SCOUT_SPEC 4.1 #7-10 and 7.3; no network was used. Every name ends in `_SYNTHETIC`; JSON files also
carry a `_SYNTHETIC` key and `.ics` files an `X-SCOUT-NOTE` line (the Cboe CSV cannot carry a marker,
since its header is pinned). The lead replaces the BLS and Cboe ones with live captures at build
step 2; nowflation's stay synthetic (its terms forbid redistributing its raw files).

| File | What it is |
|---|---|
| release_calendar_SYNTHETIC.json | `generated_at, from, source, events, past, fomc`; CPI with prior, nowflation_forecast, cleveland; NFP with prior, street; PCE with prior, kalshi; a `datetime_et` text that is never read |
| release_calendar_stale_SYNTHETIC.json | `generated_at` three days old |
| release_calendar_drift_SYNTHETIC.json | an event without `datetime_utc` (schema drift: the source is refused) |
| nowflation_SYNTHETIC.ics | the `.ics` fallback (UTC times) |
| bls_SYNTHETIC.ics | BLS schedule: no DTSTAMP, CRLF, a folded SUMMARY, `TZID=US-Eastern` and `America/New_York`, a PPI event that is left out |
| VIX_History_head_SYNTHETIC.csv | Cboe header `DATE,OPEN,HIGH,LOW,CLOSE` and a few rows, the last 10/30/2026 |

The first build's synthetic `realtime.json` and `forecasts/index.json` shapes were wrong (the live
install refused both on 2026-10-31); they were replaced by the synthetic live-layout files above.
