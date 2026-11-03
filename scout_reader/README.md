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
reader.entry_blocks("ACME", as_of)                 # 0.2.0: known bad news that blocks a Radar buy signal; adds "blocks"
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

## `entry_blocks` (0.2.0; owner, 2026-11-04)

Radar's question before a buy signal: "is there known bad news on file for this stock right now?"
Scout can only stop a buy, never start one.

```python
reader = Reader(r"E:\Scout")   # keep this one Reader: it caches the day files it has read
res = reader.entry_blocks("ACME", as_of, lookback_days=3, macro_before_min=5, macro_after_min=15)
# {"value": "true", "evidence": ["3f2a..."], "stale": False, "reasons": ["block_on_file"],
#  "blocks": [{"kind": "offering", "class": "offering", "fact_id": "3f2a...",
#              "first_seen_at": "2026-11-03T11:01:07Z", "title": "8-K - ACME CORP (Filer)"}],
#  "window": {"facts_from": "...", "first_trading_day": "2026-11-02", "macro": ["...", "..."]}}
```

- **Blocks** (`kind`): `offering`, `reverse_split`, `listing_notice`, `bankruptcy` (a wire or filing
  first seen in the last 3 trading days, counted back from as_of's trading day with the 20:00 ET
  rollover); `halt_open` (a halt that began today and has not resumed); `macro_window` (a CPI
  release, FOMC decision or jobs report from 5 minutes before to 15 minutes after its time).
- **`true`** when any block is on file, **even when Scout is stale or a source is down**: known bad
  news always blocks (`stale` then says so).
- **`false`** only when nothing is on file and Scout is fresh, every news and filing source was
  polled without a gap over the 3 trading days, the halt feed is working and the macro calendar is
  fresh. **`unknown`** otherwise; it does not block (Radar marks the signal "Scout unknown").
- Discord posts, scanner cards and AI-written labels never count.
- Speed: a Reader keeps what it parsed, keyed on each file's path, size and modification time, and
  reads only what was appended since; repeated calls take a few hundredths of a second on 10 MB day
  files. `Reader(scout_dir, cache=False)` reads every file afresh and gives the same answers.
- The module function `scout_reader.entry_blocks(reader_or_folder, symbol, as_of, ...)` accepts the
  Reader too; pass the Reader so the cache is kept between calls.

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
