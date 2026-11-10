# Alpaca answers (real, 0.3.2)

These are **real answers** from Alpaca's market-data service (`data.alpaca.markets`), cut from the
owner's probe of **10 Nov 2026 at 20:02 UTC** (15:02 ET, a Tuesday in the regular session):
`E:\Market_Data\research\reads\market_probe\probe_20261110T200236Z.json`. Only the response bodies
are kept. They are public market data for SPY, QQQ and IWM: no key, no account detail, no secret.
The probe saved each body as parsed JSON, so the files are that JSON written back out (one line,
Python's shortest form of each number, which is the form Alpaca sends).

- `snapshots_iex.json`: `GET /v2/stocks/snapshots?symbols=SPY,QQQ,IWM&feed=iex` (asked 20:02:36 UTC).
  The request Scout's live leg sends.
- `snapshots_delayed_sip.json`: the same with `feed=delayed_sip` (asked 20:02:37 UTC). Scout does not
  send it; its `prevDailyBar` closes agree with the daily bars below.
- `bars_1day_sip.json`: `GET /v2/stocks/bars?timeframe=1Day&feed=sip&adjustment=raw&limit=100`, from
  2026-10-31T20:02:36Z to 2026-11-10T19:42:36Z (asked 20:02:46 UTC). The shape of Scout's
  previous-close request; the 9 Nov bars give SPY 752.69, QQQ 679.35, IWM 282.51.
- `bars_1min_iex.json`: `GET /v2/stocks/bars?timeframe=1Min&feed=iex&adjustment=raw&limit=200` from
  2026-11-10T19:32:36Z (asked 20:02:48 UTC). The probe asked no 1-minute `feed=sip` bars, so this
  IEX answer stands in for the shape of Scout's whole-market request (the same bars shape).
- `refused_recent_sip_403.json`: the HTTP 403 body Alpaca sent for `snapshots?feed=sip` (whole-tape
  data less than 15 minutes old, which the free plan does not allow).

Used by `tests/test_alpaca_real_answers.py`. Do not edit them; cut new ones from a new probe instead.
