# scout_reader

The one piece of Scout that other bots install. It reads Scout's files (it never writes them) and
answers questions such as "was there fresh catalyst news for this stock since the last close?".
Every answer is `true`, `false` or `unknown`, with the evidence behind it. It uses the Python
standard library only and never raises an error into the bot that calls it.

## Install

In Radar or Pilot, at a pinned tag:

    pip install "git+https://github.com/JavanAppleby/Trade-Scout.git@<tag>#subdirectory=scout_reader"

## The API

Times passed in must be timezone-aware (UTC preferred). `scout_dir` is Scout's folder (the one that
holds `state\`).

```python
from scout_reader import Reader

reader = Reader(r"E:\Scout")                       # optional: now=callable, stale_s=seconds
reader.fresh_catalyst("ACME", as_of)               # gate 6: catalyst news since the prior close
reader.event_before_next_session("ACME", as_of)    # gate 5: earnings or another known event before the next open
reader.news_headline("ACME", as_of)                # any wire or filing news since the prior close; adds "classes"
reader.halt_open("ACME", as_of)                    # an open Nasdaq trading halt
reader.jason_tags("ACME", 3.20, as_of)             # on_jason_list, near_jason_level, jason_conflict, jason_market_frame
reader.market_context(as_of)                       # macro_event_next_session, inflation_regime, vix, next_macro_event
```

The same six names exist as plain functions that take `scout_dir` first, for example
`scout_reader.fresh_catalyst(r"E:\Scout", "ACME", as_of)`.

Each answer is a dict:

```python
{"value": "true" | "false" | "unknown", "evidence": [...], "stale": bool, "reasons": [...]}
```

`evidence` holds the fact ids (or due-record times) the answer rests on; `reasons` says why.
`jason_tags` and `market_context` return one such dict per tag or field.

`scout_reader.api.hold_allowed(fresh_catalyst_result, event_result)` is true only for gate 6
`true` and gate 5 `false`, both fresh. Every other pair blocks a hold.

## What the answers mean

- **Gate 6, `fresh_catalyst`**: `true` when a wire release or filing naming the stock, with a
  hold-catalyst class (FDA, trial, contract, earnings result), was published and first seen since
  the prior trading day's close (16:00 ET, 13:00 on an early close). Offerings, reverse splits,
  listing notices and "other" never count. `false` only when there is none, every monitored
  source was polled without a gap over that window, and Scout is running. `unknown` otherwise.
- **Gate 5, `event_before_next_session`**: the window runs from `as_of` to the next trading day's
  09:30 ET open (across weekends and holidays). `true` when an announced earnings date falls in
  it. `false` only when no announced date was found and the stock is clear of its EDGAR due
  window (period end + 10 to 50 days, or + 95 days after a fiscal year end), with no recent late
  filing notice, not overdue, and the calendar covering the next session. `unknown` otherwise.
- Both `false` and `unknown` block a hold.

## Fail closed

- A missing, stale, corrupt or half-written Scout file gives `unknown`, never `true`.
- Scout counts as stale (every answer `unknown`, `stale: true`) unless it wrote a heartbeat in the
  120 seconds before `as_of` (Scout's `scout_stale_s`) and that heartbeat says it is running.
- A source that is not `ok` in that heartbeat makes the answers that depend on it `unknown`.
- A record with a format this reader does not know is ignored; when a file an answer read had such
  a record, or a line it could not read, a `false` becomes `unknown`.
- No look-ahead: an answer "as of t" uses only records first seen (and published) at or before t,
  so asking again later about the same t gives the same answer.
- A time without a zone is refused (`naive_as_of`), and any error becomes `unknown` with the
  reason `error:<ExceptionType>`.
- Labels written by Scout's Claude jobs are never read by these answers.
